"""CRUD + lookup for an evaluation's allowed conversation-tag keys (the admin tag schema).

Two independent per-evaluation flags, checked in this order. `Evaluation.tags_enabled` decides
whether tagging exists here at all: off rejects every tag and the console renders no tagging surface.
`Evaluation.tags_restricted` then decides *which* keys: off means free-form regardless of the key set,
on means every key must be in the allowed set (an empty set while restricted forbids all tags).
Keeping "disabled" separate from "restricted with nothing allowed" is deliberate — they are
indistinguishable in the DB otherwise, so neither the API nor the UI could tell a deliberate opt-out
from a half-finished setup.

The policy has two entry points, because authoring a tag and replaying one already stored are
different acts. `assert_tags_allowed` rejects (400) and guards the write paths — create, PATCH, and
the tags a send request carries. `allowed_tags_only` filters and guards the prompt — the map folded
into `system_prompt`, which includes the conversation's stored tags. Rejecting there instead would
mean an admin toggling a flag leaves every already-tagged conversation unable to send, regenerate or
continue, over state its owner cannot reach once the console hides the tagging surface.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.conversations.models import Message
from app.core.conversations.models import recorded_tag_context
from app.core.conversations.schemas import MAX_TAGS
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationTagKey
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.helpers import sanitise_single_line
from app.core.logging import get_logger

logger = get_logger(__name__)


async def list_tag_keys(session: AsyncSession, evaluation_id: UUID) -> list[EvaluationTagKey]:
    """The evaluation's live allowed-tag keys, ordered by key."""
    result = await session.execute(
        EvaluationTagKey.live_select()
        .where(col(EvaluationTagKey.evaluation_id) == evaluation_id)
        .order_by(col(EvaluationTagKey.key))
    )
    return list(result.scalars().all())


async def allowed_tag_keys(session: AsyncSession, evaluation_id: UUID) -> set[str]:
    """The set of live allowed-tag keys for an evaluation (empty = unrestricted)."""
    result = await session.execute(
        select(col(EvaluationTagKey.key)).where(
            col(EvaluationTagKey.evaluation_id) == evaluation_id,
            col(EvaluationTagKey.deleted_at).is_(None),
        )
    )
    return set(result.scalars().all())


@dataclass(frozen=True)
class TagFoldPolicy:
    """An evaluation's tag policy, resolved once, so a caller can answer "was this tag sent?" offline.

    The read side needs the same answer `allowed_tags_only` computes at the fold, but per row and
    without a query each: an export or a projection that prints a stored tag without saying the
    policy filters it out claims the model receives context it doesn't.

    Judged against the policy as it stands now, not against what was folded at send time. An
    assistant reply records the map its prompt actually carried (`tag_context`), so for those rows
    "was this sent?" has an exact answer already; this policy remains the only answer for a user
    message's own tags, for a reply generated before that record existed, and for one whose
    finalize never ran (crash or reaper sweep).
    """

    enabled: bool
    restricted: bool
    allowed: frozenset[str]

    def unsent(self, tags: Mapping[str, str]) -> list[str]:
        """The keys of `tags` this policy keeps out of the prompt, sorted — empty when all of it folds.

        Mirrors `allowed_tags_only` (tagging off drops all; restricted drops keys outside the allowed
        set) plus `render_tag_context`'s rule that a value carrying no text is nothing to send.
        """
        if not tags:
            return []
        if not self.enabled:
            return sorted(tags)
        return sorted(
            key
            for key, value in tags.items()
            # `str(...)` like `render_tag_context`: a value written by an import or direct SQL need not
            # be a string, and an export must not die on one.
            if not sanitise_single_line(str(value)) or (self.restricted and key not in self.allowed)
        )

    def unsent_for_message(self, message: Message, tags: Mapping[str, str]) -> list[str]:
        """Like `unsent`, but historical via `message`'s own record; a sanitise-key collision reads both as sent."""
        record = recorded_tag_context(message)
        if record is None:
            return self.unsent(tags)
        sent_keys = {sanitise_single_line(key) for key in record}
        return sorted(key for key in tags if sanitise_single_line(key) not in sent_keys)


async def load_tag_fold_policy(session: AsyncSession, evaluation_id: UUID) -> TagFoldPolicy:
    """Resolve an evaluation's fold policy in at most two queries (one when unrestricted).

    For read paths that judge many rows against it — one resolve per export, not per row.
    """
    enabled, restricted = await _tagging_policy(session, evaluation_id)
    allowed = await allowed_tag_keys(session, evaluation_id) if restricted else set()
    return TagFoldPolicy(enabled=enabled, restricted=restricted, allowed=frozenset(allowed))


async def _tagging_policy(session: AsyncSession, evaluation_id: UUID) -> tuple[bool, bool]:
    """The evaluation's ``(tags_enabled, tags_restricted)`` pair.

    Raises:
        NotFoundError: If no live evaluation has this id. Fail closed: every caller resolves a live,
            visible parent first, so a missing row means the caller's assumptions are broken.
    """
    flags = (
        await session.execute(
            select(col(Evaluation.tags_enabled), col(Evaluation.tags_restricted)).where(
                col(Evaluation.id) == evaluation_id,
                col(Evaluation.deleted_at).is_(None),
            )
        )
    ).one_or_none()
    if flags is None:
        raise NotFoundError("Evaluation not found.")
    enabled, restricted = flags
    return bool(enabled), bool(restricted)


async def allowed_tags_only(session: AsyncSession, evaluation_id: UUID, tags: dict[str, str]) -> dict[str, str]:
    """The subset of ``tags`` the evaluation's policy lets reach the model.

    Drops rather than rejects, because this guards the fold into the prompt: the map carries the
    conversation's stored tags, so a tightened policy must stop them reaching the model without
    failing the turn. Authoring is the other entry point — see `assert_tags_allowed`.

    Deliberately does **not** cap the merged map. Each layer is already bounded on its own
    (`MAX_TAGS` keys and 10 KiB per side at the schema edge), so the union is bounded too — at twice
    that, which is a prompt cost, not an unbounded one. A cap here would have to be enforced against
    the *filtered* map to avoid rejecting keys the policy drops anyway, and any truncation would have
    to discard the layer the contract says wins (message tags override per key) — two ways to be
    wrong about the same ceiling, in exchange for a bound the edges already provide.
    """
    if not tags:
        return {}
    enabled, restricted = await _tagging_policy(session, evaluation_id)
    if not enabled:
        # Observable, like `conversations.history.image_missing`: for an artefact whose value is
        # "what did the model see", a silent drop is a gap the transcript cannot explain later.
        logger.warning(
            "conversations.tags.dropped",
            evaluation_id=str(evaluation_id),
            reason="tagging_disabled",
            dropped=sorted(tags),
        )
        return {}
    if restricted:
        allowed = await allowed_tag_keys(session, evaluation_id)
        dropped = sorted(set(tags) - allowed)
        if dropped:
            logger.warning(
                "conversations.tags.dropped",
                evaluation_id=str(evaluation_id),
                reason="not_in_allowed_set",
                dropped=dropped,
            )
        tags = {key: value for key, value in tags.items() if key in allowed}
    return dict(tags)


async def assert_tags_allowed(session: AsyncSession, evaluation_id: UUID, tags: dict[str, str]) -> None:
    """Reject tags the evaluation's tagging policy disallows — a no-op when it allows them.

    No-op when ``tags`` is empty, and (with tagging enabled) when ``tags_restricted`` is off. With
    tagging disabled every tag is rejected; while restricted, every key must be in the allowed set
    (an empty set forbids all tags).

    Raises:
        BadRequestError: If the evaluation has tagging disabled, or restricts tags and ``tags``
            carries a key outside the allowed set (mirrors the allowed-model subset check on model
            assignment).
        NotFoundError: If no live evaluation has this id.
    """
    if not tags:
        return
    enabled, restricted = await _tagging_policy(session, evaluation_id)
    if not enabled:
        raise BadRequestError("Tagging is disabled for this evaluation.")
    if not restricted:
        return
    allowed = await allowed_tag_keys(session, evaluation_id)
    disallowed = sorted(set(tags) - allowed)
    if disallowed:
        raise BadRequestError(f"Tag key(s) not allowed for this evaluation: {', '.join(disallowed)}.")


async def add_tag_key(session: AsyncSession, *, evaluation_id: UUID, key: str) -> EvaluationTagKey:
    """Allow ``key`` on the evaluation.

    Raises:
        ConflictError: If the key is already allowed, or the evaluation already holds `MAX_TAGS`
            keys — the same ceiling a single conversation's tag map has, so an allow-list longer
            than that is weight the write path re-reads on every create / PATCH / send without any
            conversation being able to use it. Unlike the uniqueness rule this cap has no index
            behind it, so concurrent adds can overshoot it by the number of racing requests — a row
            past the ceiling, which no conversation can use, since a conversation's own tag map is
            capped at the same number.
    """
    # One read settles both preconditions: the live key set answers "already allowed?" and "full?".
    live = await allowed_tag_keys(session, evaluation_id)
    if key in live:
        raise ConflictError(f"Tag key {key!r} is already allowed for this evaluation.")
    if len(live) >= MAX_TAGS:
        raise ConflictError(f"Evaluation already allows the maximum of {MAX_TAGS} tag keys.")
    row = EvaluationTagKey(evaluation_id=evaluation_id, key=key)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The pre-read above loses a concurrent race; the partial-unique index is the arbiter, so the
        # loser gets the documented 409 rather than a 500.
        raise ConflictError(f"Tag key {key!r} is already allowed for this evaluation.") from exc
    await session.refresh(row)
    return row


async def remove_tag_key(session: AsyncSession, *, evaluation_id: UUID, key: str, by_id: UUID) -> EvaluationTagKey:
    """Soft-delete the allowed ``key`` and return the removed row (404 if not defined)."""
    row = (
        await session.execute(
            EvaluationTagKey.live_select().where(
                col(EvaluationTagKey.evaluation_id) == evaluation_id,
                col(EvaluationTagKey.key) == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Tag key {key!r} is not defined for this evaluation.")
    row.soft_delete(by_id)
    session.add(row)
    await session.flush()
    return row
