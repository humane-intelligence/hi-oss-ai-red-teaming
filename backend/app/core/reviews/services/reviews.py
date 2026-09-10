"""Review service — pure async functions over an `AsyncSession`.

A `Review` is one reviewer's assignment-plus-verdict on a flagged submission
(`MessageFlag`). Assignment (`assign_reviewer`) creates a `pending` review under
the one-review-per-reviewer rule; the verdict (`update_review`) flips `status`
to `approved` / `rejected`; unassign (`unassign_review`) soft-deletes the row.

Reads are scoped by `scope_reviews` (group visibility + the reviewer-vs-author
split); the awaiting-review `review_queue` lists flags whose completed reviews
fall short of their scenario's `required_reviews`, each with the reviews
assigned so far.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.annotations.enums import FlagStatus
from app.core.annotations.models import MessageFlag
from app.core.auth.models import User
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.services.messages import list_conversation_messages
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.services.annotators import annotator_pool_predicate
from app.core.evaluations.services.annotators import is_assignable_annotator
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.helpers import escape_like
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.reviews.access import scope_review_flags
from app.core.reviews.access import scope_reviews
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.filters import ReviewFilters
from app.core.reviews.filters import ReviewOrderBy
from app.core.reviews.filters import ReviewQueueFilters
from app.core.reviews.models import Review
from app.core.reviews.schemas import ReviewResponse
from app.core.reviews.schemas import ReviewUpdateChanges
from app.core.soft_delete import with_live

_COMPLETED_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)


async def _resolve_assignable_flag(
    session: AsyncSession, flag_id: UUID, *, caller_id: UUID, can_manage: bool
) -> MessageFlag:
    """Resolve a live flag the caller may assign reviewers on.

    Review writes (assign here, verdict/unassign via `get_review`) authorize on
    group *visibility* plus the JWT `reviews:*` permission — deliberately not the
    in-group write authority `assert_group_write_access` gates scenarios/tasks
    with. Reviewing is the annotator's cross-group job: a `public` group is open
    to any annotator, an `invitation_only` group is visible only to its members,
    so visibility already scopes writes to the right pool without a membership
    check. Locks the flag row (`for_update`) so a racing duplicate assignment to
    the same flag resolves to a clean 409 rather than a unique-constraint error.

    Raises:
        NotFoundError: If no live flag ``flag_id`` is visible to the caller.
    """
    statement = join_visible_evaluation_group(
        MessageFlag.live_select().where(col(MessageFlag.id) == flag_id),
        col(MessageFlag.evaluation_id),
        caller_id=caller_id,
        can_manage=can_manage,
    ).with_for_update(of=MessageFlag)
    flag = (await session.execute(statement)).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"Message flag {flag_id} not found.")
    return flag


async def assign_reviewer(
    session: AsyncSession, flag_id: UUID, *, reviewer_id: UUID, caller_id: UUID, can_manage: bool
) -> Review:
    """Assign ``reviewer_id`` to review flag ``flag_id`` — a new `pending` review.

    Enforces the one-review-per-reviewer rule and the no-self-review guard. The
    flag must be visible to the caller, must still be `pending` (a decided flag
    takes no new reviewers), and ``reviewer_id`` must be in the flag group's
    assignable-reviewer pool (the same pool the `/annotators` search exposes):
    holders of the `reviews:annotate` capability scoped by the group's access
    level — global holders and/or the group's in-group holders.

    Raises:
        NotFoundError: If the flag isn't visible to the caller, ``reviewer_id``
            isn't a live user, or that user isn't in the assignable-reviewer pool
            for the flag's group.
        ConflictError: If the flag already carries a verdict (decided), the
            reviewer authored it (no self-review), or is already assigned.
    """
    flag = await _resolve_assignable_flag(session, flag_id, caller_id=caller_id, can_manage=can_manage)
    if flag.status != FlagStatus.PENDING:
        raise ConflictError("Cannot assign a reviewer to an already-decided submission.")
    if reviewer_id == flag.created_by_id:
        raise ConflictError("A flag's author cannot review their own submission.")
    reviewer = (await session.execute(User.live_select().where(col(User.id) == reviewer_id))).scalar_one_or_none()
    if reviewer is None:
        raise NotFoundError(f"User {reviewer_id} not found.")
    group = (
        await session.execute(EvaluationGroup.live_select().where(col(EvaluationGroup.id) == flag.evaluation_group_id))
    ).scalar_one_or_none()
    if group is None or not await is_assignable_annotator(session, group, reviewer_id):
        raise NotFoundError(f"User {reviewer_id} is not an assignable reviewer for this submission.")
    existing = (
        await session.execute(
            Review.live_select().where(col(Review.message_flag_id) == flag_id, col(Review.reviewer_id) == reviewer_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Reviewer is already assigned to this submission.")
    review = Review(
        message_flag_id=flag_id, reviewer_id=reviewer_id, assigned_by_id=caller_id, evaluation_id=flag.evaluation_id
    )
    session.add(review)
    await session.flush()
    await session.refresh(review, attribute_names=["created_at", "updated_at"])
    return review


async def list_assignable_reviewers(
    session: AsyncSession,
    flag_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    search: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[User], int]:
    """Return one page of users assignable as a reviewer to submission ``flag_id``.

    The picker counterpart of the `assign_reviewer` gate: while the flag is still
    `pending` (a decided flag takes no new reviewers, so the pool is empty), the
    flag group's assignable-reviewer pool (`annotator_pool_predicate` — the exact
    rule `is_assignable_annotator` enforces) minus the flag's author (no
    self-review) and anyone already carrying a live review on it
    (one-review-per-reviewer), so every candidate the picker offers passes
    `assign_reviewer` rather than being rejected (404/409).

    Raises:
        NotFoundError: If no live flag ``flag_id`` is visible to the caller.
    """
    flag = (
        await session.execute(
            join_visible_evaluation_group(
                MessageFlag.live_select().where(col(MessageFlag.id) == flag_id),
                col(MessageFlag.evaluation_id),
                caller_id=caller_id,
                can_manage=can_manage,
            )
        )
    ).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"Message flag {flag_id} not found.")
    if flag.status != FlagStatus.PENDING:
        return [], 0
    group = (
        await session.execute(EvaluationGroup.live_select().where(col(EvaluationGroup.id) == flag.evaluation_group_id))
    ).scalar_one_or_none()
    if group is None:
        return [], 0
    already_assigned = select(col(Review.reviewer_id)).where(
        col(Review.message_flag_id) == flag_id, col(Review.deleted_at).is_(None)
    )
    candidates = User.live_select().where(
        annotator_pool_predicate(group),
        col(User.id) != flag.created_by_id,
        col(User.id).notin_(already_assigned),
    )
    if search:
        like = f"%{escape_like(search)}%"  # escape LIKE metacharacters — a raw % would match everyone
        candidates = candidates.where(
            or_(
                col(User.email).ilike(like, escape="\\"),
                col(User.first_name).ilike(like, escape="\\"),
                col(User.last_name).ilike(like, escape="\\"),
            )
        )
    candidates = candidates.order_by(col(User.email), col(User.id))
    return await paginate(session, candidates, limit=limit, offset=offset)


# Terminal states: work in one of these is over, so a pending review on it is not open load.
_DEAD_EVALUATION_STATUSES = (EvaluationStatus.COMPLETED, EvaluationStatus.REJECTED)
_DEAD_GROUP_STATUSES = (PublicationStatus.INACTIVE, PublicationStatus.NOT_APPROVED)


async def count_active_reviews(session: AsyncSession, user_ids: list[UUID]) -> dict[UUID, int]:
    """Open review workload per user: pending reviews on work that is still running.

    Counts live `pending` reviews whose whole parent chain is live — flag, conversation, evaluation
    and group, the same chain `scope_reviews` reads through — and whose evaluation and group are not
    in a terminal state. Users with no such review are absent from the mapping, so callers supply
    the zero.

    Narrower than the review queue on purpose: the queue lists anything still short of its
    `required_reviews`, including work inside a finished evaluation or a retired group, so a
    reviewer can hold queue rows here and still count zero. The queue answers "what is unfinished",
    this answers "what is worth assigning more of".
    """
    if not user_ids:
        return {}
    statement = (
        select(col(Review.reviewer_id), func.count())
        .select_from(Review)
        .join(MessageFlag, col(Review.message_flag_id) == col(MessageFlag.id))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .join(Evaluation, col(MessageFlag.evaluation_id) == col(Evaluation.id))
        .join(EvaluationGroup, col(MessageFlag.evaluation_group_id) == col(EvaluationGroup.id))
        .where(
            col(Review.reviewer_id).in_(user_ids),
            col(Review.deleted_at).is_(None),
            col(Review.status) == ReviewStatus.PENDING,
            col(MessageFlag.deleted_at).is_(None),
            # A model-unassign cascade tombstones the conversation and leaves the flag live; every
            # review surface hides such a review, so counting it would report load nobody can act on.
            col(Conversation.deleted_at).is_(None),
            col(Evaluation.deleted_at).is_(None),
            col(Evaluation.status).notin_(_DEAD_EVALUATION_STATUSES),
            col(EvaluationGroup.deleted_at).is_(None),
            col(EvaluationGroup.status).notin_(_DEAD_GROUP_STATUSES),
        )
        .group_by(col(Review.reviewer_id))
    )
    return dict((await session.execute(statement)).tuples().all())


async def get_review(
    session: AsyncSession,
    review_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    for_update: bool = False,
) -> Review:
    """Fetch one live review ``review_id`` the caller may read.

    A review the caller can't see (group not visible, parent flag soft-deleted,
    or — for a non-reviewer — a flag they didn't author) reads as missing (404).
    Mutation paths pass ``for_update=True`` to lock the row.

    Raises:
        NotFoundError: If no such review is visible to the caller.
    """
    statement = scope_reviews(
        Review.live_select().where(col(Review.id) == review_id),
        caller_id=caller_id,
        can_manage=can_manage,
        can_review=can_review,
    )
    if for_update:
        statement = statement.with_for_update(of=Review)
    review = (await session.execute(statement)).scalar_one_or_none()
    if review is None:
        raise NotFoundError(f"Review {review_id} not found.")
    return review


async def resolve_reviewer_emails(session: AsyncSession, reviews: list[Review]) -> dict[UUID, str]:
    """Batch-resolve `reviewer_id -> email` for a set of reviews in one query.

    The FE can't resolve a reviewer_id to an email off a bounded users list (large deployments have
    reviewers well past any page), so the reviewer's email is authoritative here. Split out so a caller
    holding reviews across several parents (e.g. the review queue) resolves them all in one query
    instead of one per parent.
    """
    ids = {r.reviewer_id for r in reviews}
    if not ids:
        return {}
    # Plain `select` (not `live_select`): a soft-deleted reviewer still owns the assignment, so display
    # keeps their email for historical attribution — deliberately asymmetric with `_notify`, which uses
    # `live_select` because we don't email a deleted account.
    users = (await session.execute(select(User).where(col(User.id).in_(ids)))).scalars().all()
    return {user.id: user.email for user in users}


async def project_reviews(session: AsyncSession, reviews: list[Review]) -> list[ReviewResponse]:
    """Project reviews to `ReviewResponse`, batch-resolving each reviewer's email in one query."""
    emails = await resolve_reviewer_emails(session, reviews)
    return [ReviewResponse.from_model(r, reviewer_email=emails.get(r.reviewer_id)) for r in reviews]


async def project_review(session: AsyncSession, review: Review) -> ReviewResponse:
    """Single-review projection with the reviewer's email resolved (see `project_reviews`)."""
    return (await project_reviews(session, [review]))[0]


async def list_reviews(  # noqa: PLR0913 — keyword-only args mirror the listing's scope, filters, page and window; a carrier object would just shift the surface area
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    filters: ReviewFilters,
    order_by: ReviewOrderBy,
    limit: int,
    offset: int,
    deleted: bool = False,
    deleted_cutoff: datetime,
) -> tuple[list[Review], int]:
    """Return one page of reviews the caller may read, matching ``filters``.

    The visibility + reviewer/author scope is applied before the user filters, so
    a filter can only narrow it.

    ``deleted`` swaps in the tombstones still inside the restore window, keeping
    ``order_by`` as given (pass `-deleted_at` for newest-first). Scoped to the caller's own
    unassignments unless they hold the break-glass, which is what keeps the list free of
    rows whose restore would 403: a non-manager could only have unassigned a review that
    was pending, or one carrying their own verdict — exactly what `restore_review` accepts
    back. A pending tombstone additionally has to still be reassignable (flag undecided,
    reviewer live) to appear; pool membership is re-checked only by the restore
    (`assert_review_reassignable`) — the one condition a flat, cross-group listing cannot
    express. The visibility spine still applies, so a tombstoned parent flag or
    conversation hides the review either way.
    ``deleted_cutoff`` comes from the route, like `get_restorable_review`'s, so the listing
    and the restore cannot disagree on the window.
    """
    base = (
        deleted_select(Review, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        if deleted
        else Review.live_select()
    )
    statement = scope_reviews(base, caller_id=caller_id, can_manage=can_manage, can_review=can_review)
    if deleted:
        statement = statement.where(
            or_(
                col(Review.status) != ReviewStatus.PENDING,
                and_(
                    col(MessageFlag.status) == FlagStatus.PENDING,
                    exists(
                        select(col(User.id)).where(
                            col(User.id) == col(Review.reviewer_id), col(User.deleted_at).is_(None)
                        )
                    ),
                ),
            )
        )
    if filters.message_flag_id is not None:
        statement = statement.where(col(Review.message_flag_id) == filters.message_flag_id)
    if filters.evaluation_id is not None:
        statement = statement.where(col(Review.evaluation_id) == filters.evaluation_id)
    if filters.reviewer_id is not None:
        statement = statement.where(col(Review.reviewer_id) == filters.reviewer_id)
    if filters.status is not None:
        statement = statement.where(col(Review.status) == filters.status)
    if filters.created_from is not None:
        statement = statement.where(col(Review.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(Review.created_at) <= filters.created_to)
    statement = apply_order_by(statement, Review, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def update_review(session: AsyncSession, review: Review, changes: ReviewUpdateChanges) -> Review:
    """Apply ``changes`` to ``review`` — writes only fields in `changes.model_fields_set`.

    Records the verdict (`successful_exploit` / `unique_exploit` /
    `valid_submission` / `number_prompts` / `notes`) and flips `status` to
    `approved` / `rejected`. The reviewer-only write gate is the route's job.

    An already-decided review stays editable — a reviewer may correct their
    verdict; there is no terminal-state lock because the aggregate flag verdict
    is not derived from reviews yet. Introduce immutability (or versioning) when
    consensus scoring consumes these rows.
    """
    for field in changes.model_fields_set:
        setattr(review, field, getattr(changes, field))
    session.add(review)
    await session.flush()
    await session.refresh(review, attribute_names=["updated_at"])
    return review


async def unassign_review(session: AsyncSession, review: Review, *, by_id: UUID) -> Review:
    """Soft-delete ``review`` by stamping `deleted_at` (unassign the reviewer)."""
    review.soft_delete(by_id)
    session.add(review)
    await session.flush()
    await session.refresh(review)
    return review


async def get_restorable_review(
    session: AsyncSession,
    review_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    deleted_cutoff: datetime,
) -> Review:
    """Fetch the tombstoned review ``review_id`` inside the restore window.

    Scoped exactly like the deleted listing — the caller's own unassignments unless they
    hold the break-glass — and through `scope_reviews`, so a review whose parent flag or
    conversation is itself tombstoned reads as missing rather than confirming it exists.
    Restoring the parent is what brings those back.

    Raises:
        NotFoundError: Never deleted, deleted longer than the window ago, deleted by
            someone else, or not visible to the caller.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = scope_reviews(
        deleted_select(Review, deleted_cutoff, deleted_by=None if can_manage else caller_id).where(
            col(Review.id) == review_id
        ),
        caller_id=caller_id,
        can_manage=can_manage,
        can_review=can_review,
    )
    statement = statement.with_for_update(of=Review).execution_options(populate_existing=True)
    review = (await session.execute(statement)).scalar_one_or_none()
    if review is None:
        raise NotFoundError(f"No restorable review {review_id} was deleted within the restore window.")
    return review


async def assert_review_reassignable(session: AsyncSession, review: Review) -> None:
    """Re-run `assign_reviewer`'s preconditions before reviving a *pending* review.

    A pending review is a working assignment, so its restore must yield one somebody can
    act on: the flag still takes verdicts and the reviewer is a live member of the group's
    assignable pool. A decided review is a record — it rides back untouched even if the
    reviewer has since been deleted or left the pool (`resolve_reviewer_emails` already
    renders such reviewers for historical attribution).

    Raises:
        NotFoundError: The parent flag is gone, the reviewer is no longer a live user,
            or no longer in the group's assignable-reviewer pool.
        ConflictError: The flag was decided while the review was unassigned.
    """
    if review.status != ReviewStatus.PENDING:
        return
    flag = (
        await session.execute(MessageFlag.live_select().where(col(MessageFlag.id) == review.message_flag_id))
    ).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"No restorable review {review.id} was deleted within the restore window.")
    if flag.status != FlagStatus.PENDING:
        raise ConflictError("Cannot restore a pending review of an already-decided submission.")
    reviewer = (
        await session.execute(User.live_select().where(col(User.id) == review.reviewer_id))
    ).scalar_one_or_none()
    if reviewer is None:
        raise NotFoundError(f"Reviewer {review.reviewer_id} is no longer a live user.")
    group = (
        await session.execute(EvaluationGroup.live_select().where(col(EvaluationGroup.id) == flag.evaluation_group_id))
    ).scalar_one_or_none()
    if group is None or not await is_assignable_annotator(session, group, review.reviewer_id):
        raise NotFoundError(f"User {review.reviewer_id} is not an assignable reviewer for this submission.")


async def restore_review(session: AsyncSession, review: Review) -> Review:
    """Clear ``review``'s tombstone, putting the reviewer back on the flag.

    The verdict fields ride along untouched, which is the point: unassigning a decided
    review and restoring it returns the same verdict rather than a blank assignment. The
    caller owns the decided-review gate, mirroring `unassign_review`'s.

    Raises:
        ConflictError: If the reviewer has since been re-assigned to the same flag — the
            partial unique index on `(message_flag_id, reviewer_id)` covers live rows only.
    """
    await restore_row(
        session,
        review,
        conflict_message="That reviewer is already assigned to this submission; unassign the live review first.",
    )
    await session.refresh(review, attribute_names=["updated_at"])
    return review


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One awaiting-review flag with its required-review count, completed count, and reviews."""

    flag: MessageFlag
    required_reviews: int
    completed_reviews: int
    reviews: list[Review]


async def review_queue(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    filters: ReviewQueueFilters,
    limit: int,
    offset: int,
) -> tuple[list[QueueEntry], int]:
    """Return one page of flags awaiting review (completed reviews < required count).

    "Awaiting" is computed in SQL against `Scenario.required_reviews` (coalesced
    to 1 when the flag targets no live scenario). Each page entry is loaded with
    its flagged messages and the reviews assigned to it so far. ``filters.unassigned``
    narrows further, to flags no live review points at.
    """
    completed = (
        select(func.count())
        .select_from(Review)
        .where(
            col(Review.message_flag_id) == col(MessageFlag.id),
            col(Review.deleted_at).is_(None),
            col(Review.status).in_(_COMPLETED_STATUSES),
        )
        .scalar_subquery()
    )
    required = func.coalesce(col(Scenario.required_reviews), 1)
    statement = scope_review_flags(
        MessageFlag.live_select(), caller_id=caller_id, can_manage=can_manage, can_review=can_review
    ).outerjoin(Scenario, and_(col(MessageFlag.scenario_id) == col(Scenario.id), col(Scenario.deleted_at).is_(None)))
    if filters.evaluation_id is not None:
        statement = statement.where(col(MessageFlag.evaluation_id) == filters.evaluation_id)
    if filters.evaluation_group_id is not None:
        statement = statement.where(col(MessageFlag.evaluation_group_id) == filters.evaluation_group_id)
    if filters.scenario_id is not None:
        statement = statement.where(col(MessageFlag.scenario_id) == filters.scenario_id)
    if filters.unassigned:
        statement = statement.where(
            ~exists(
                select(1)
                .select_from(Review)
                .where(col(Review.message_flag_id) == col(MessageFlag.id), col(Review.deleted_at).is_(None))
            )
        )
    statement = (
        statement.where(completed < required)
        .order_by(col(MessageFlag.created_at).desc(), col(MessageFlag.id))
        .options(
            selectinload(MessageFlag.messages),  # ty: ignore[invalid-argument-type]
            with_live(Message),
        )
    )
    flags, total = await paginate(session, statement, limit=limit, offset=offset)
    if not flags:
        return [], total

    flag_ids = [flag.id for flag in flags]
    review_rows = (
        (
            await session.execute(
                Review.live_select()
                .where(col(Review.message_flag_id).in_(flag_ids))
                .order_by(col(Review.created_at), col(Review.id))
            )
        )
        .scalars()
        .all()
    )
    reviews_by_flag: dict[UUID, list[Review]] = defaultdict(list)
    for review in review_rows:
        reviews_by_flag[review.message_flag_id].append(review)

    scenario_ids = {flag.scenario_id for flag in flags if flag.scenario_id is not None}
    required_by_scenario: dict[UUID, int] = {}
    if scenario_ids:
        scenarios = (
            (await session.execute(Scenario.live_select().where(col(Scenario.id).in_(scenario_ids)))).scalars().all()
        )
        required_by_scenario = {scenario.id: scenario.required_reviews for scenario in scenarios}

    entries = [
        QueueEntry(
            flag=flag,
            required_reviews=required_by_scenario.get(flag.scenario_id, 1) if flag.scenario_id is not None else 1,
            completed_reviews=sum(1 for review in reviews_by_flag[flag.id] if review.status in _COMPLETED_STATUSES),
            reviews=reviews_by_flag[flag.id],
        )
        for flag in flags
    ]
    return entries, total


async def get_submission_detail(
    session: AsyncSession,
    flag_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    limit: int,
    offset: int,
) -> tuple[MessageFlag, set[UUID], list[Review], int]:
    """Fetch one flagged submission the caller may review, with a page of its reviews.

    Visibility is the review scope (`scope_review_flags`) — identical to the
    queue: a reviewer / break-glass manager sees any flag in a visible group, a
    red-teamer only their own. The flagged messages are eager-loaded; the reviews
    are paginated (the flag's visibility already gates them) and the total count
    is returned alongside.

    The second element is the subset of flagged message ids that another message
    later superseded (regenerate/continue): they stay in the flag's set for
    provenance but drop out of the live transcript (`submission_conversation_messages`),
    so the detail marks them — otherwise a reviewer can't locate them in context.

    Raises:
        NotFoundError: If no such flag is visible to the caller.
    """
    statement = scope_review_flags(
        MessageFlag.live_select().where(col(MessageFlag.id) == flag_id),
        caller_id=caller_id,
        can_manage=can_manage,
        can_review=can_review,
    ).options(
        selectinload(MessageFlag.messages),  # ty: ignore[invalid-argument-type]
        with_live(Message),
    )
    flag = (await session.execute(statement)).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"Submission {flag_id} not found.")
    flagged_ids = [message.id for message in flag.messages]
    superseded_ids: set[UUID] = set()
    if flagged_ids:
        rows = await session.execute(
            select(col(Message.replaces_message_id)).where(col(Message.replaces_message_id).in_(flagged_ids))
        )
        superseded_ids = {mid for mid in rows.scalars().all() if mid is not None}
    reviews, total = await paginate(
        session,
        Review.live_select()
        .where(col(Review.message_flag_id) == flag_id)
        .order_by(col(Review.created_at), col(Review.id)),
        limit=limit,
        offset=offset,
    )
    return flag, superseded_ids, reviews, total


async def submission_conversation_messages(
    session: AsyncSession,
    flag_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    can_review: bool,
    limit: int,
    offset: int,
) -> tuple[list[Message], int]:
    """Paginated parent-conversation transcript for a submission the caller may review.

    Review-scoped through the flag's visibility (`scope_review_flags`) — the
    conversations message-history endpoint is owner-scoped, so a reviewer who
    isn't the conversation's owner reads its full transcript here instead.

    Raises:
        NotFoundError: If no such flag is visible to the caller.
    """
    flag = (
        await session.execute(
            scope_review_flags(
                MessageFlag.live_select().where(col(MessageFlag.id) == flag_id),
                caller_id=caller_id,
                can_manage=can_manage,
                can_review=can_review,
            )
        )
    ).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"Submission {flag_id} not found.")
    return await list_conversation_messages(session, flag.conversation_id, limit=limit, offset=offset)
