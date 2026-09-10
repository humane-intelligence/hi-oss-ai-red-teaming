"""Message-flag service — pure async functions over an `AsyncSession`.

Message flags are **owner-scoped** *and* gated by the parent group's visibility,
exactly like conversations: a caller reads, updates, and deletes only flags they
authored (`created_by_id == caller_id`) whose denormalised parents are live and
whose group is still visible to them. The `evaluation_groups:manage` break-glass
(``can_manage``) lifts both the owner and visibility predicates; parent
**liveness** (conversation, evaluation, group) is enforced regardless, so a
soft-deleted ancestor hides the flag for everyone.

A flag is anchored to one `conversation_id` and selects an explicit set of that
conversation's messages (the `flagged_messages` link rows). Because the ancestry
is denormalised onto the flag at create time, every read needs only the flag's
own columns; the selected messages' base data is eager-loaded via the viewonly
`MessageFlag.messages` relationship (`selectinload` + `with_live(Message)` — one
extra query per page), so the response carries live `Message` rows, not bare ids.

A flag's linked messages are always live — flags can't be added to deleted
messages (create checks liveness) and a flagged message can't be deleted — so
`with_live(Message)` on the projection is belt-and-braces, and the `message_id`
list filter matches the `flagged_messages` link rows directly.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.annotations.filters import MessageFlagFilters
from app.core.annotations.filters import MessageFlagOrderBy
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.annotations.schemas import MessageFlagCreate
from app.core.annotations.schemas import MessageFlagUpdateChanges
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.services.messages import assert_messages_in_conversation
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.access import join_conversation_scoped
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Task
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


def _scope(statement: Select[tuple[MessageFlag]], *, caller_id: UUID, can_manage: bool) -> Select[tuple[MessageFlag]]:
    """Constrain to the caller's own, group-visible flags on a live conversation.

    The shared `join_conversation_scoped` scope — see it for what ``can_manage``
    lifts and what stays enforced regardless.
    """
    return join_conversation_scoped(statement, MessageFlag, caller_id=caller_id, can_manage=can_manage)


async def resolve_conversation_context(
    session: AsyncSession, conversation_id: UUID, *, caller_id: UUID
) -> tuple[Conversation, UUID]:
    """Resolve a flaggable conversation to itself and its evaluation-group id.

    Walks `conversation → evaluation → group`, all live, under the caller's
    ownership and the group's visibility. The returned conversation supplies the
    denormalised `evaluation_id` / `scenario_id` at create time; the group id is
    returned alongside since it isn't carried on the conversation row.

    Authoring is owner-only: you can only flag a conversation you own, even with
    the `evaluation_groups:manage` break-glass (which still lifts the owner scope
    on read/update/delete — a manager can review and act on others' flags, just
    not author one on their behalf, since `created_by_id` is the author and
    owner-scoped reads would otherwise hide the manager's flag from the owner).
    A conversation the caller doesn't own (or can't see) reads as missing —
    addressing it should not confirm its existence. The note sibling
    (`services/notes.resolve_note_conversation`) carries no such ownership
    predicate.

    Raises:
        NotFoundError: If no live conversation ``conversation_id`` exists under a
            live, caller-visible group owned by the caller.
    """
    statement = (
        select(Conversation, col(EvaluationGroup.id))
        .select_from(Conversation)
        .join(Evaluation, col(Conversation.evaluation_id) == col(Evaluation.id))
        .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(Conversation.id) == conversation_id)
        .where(col(Conversation.deleted_at).is_(None))
        .where(col(Evaluation.deleted_at).is_(None))
        .where(col(EvaluationGroup.deleted_at).is_(None))
        .where(group_visible_to(caller_id))
        .where(col(Conversation.user_id) == caller_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    conversation, evaluation_group_id = row
    return conversation, evaluation_group_id


async def get_flag(
    session: AsyncSession,
    flag_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
) -> MessageFlag:
    """Fetch one live message flag ``flag_id`` visible to the caller, with its message set.

    A flag authored by another user — or whose parent group is no longer visible
    to the caller, or whose conversation / evaluation / group is soft-deleted —
    reads as missing (404, no existence leak) unless ``can_manage`` lifts the
    owner / visibility predicates. Mutation paths pass ``for_update=True`` to lock
    the flag row for a same-transaction write.

    Raises:
        NotFoundError: If no such flag is visible to the caller.
    """
    statement = _scope(
        MessageFlag.live_select().where(col(MessageFlag.id) == flag_id),
        caller_id=caller_id,
        can_manage=can_manage,
    ).options(selectinload(MessageFlag.messages), with_live(Message))  # ty: ignore[invalid-argument-type]
    if for_update:
        # Lock only the flag row — the joined ancestry are visibility predicates.
        statement = statement.with_for_update(of=MessageFlag)
    flag = (await session.execute(statement)).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"Message flag {flag_id} not found.")
    return flag


async def list_flags(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    filters: MessageFlagFilters,
    order_by: MessageFlagOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[MessageFlag], int]:
    """Return one page of the caller's flags matching ``filters``, each with its message set.

    The owner + group-visibility scope is applied *before* the user filters so a
    filter can never widen it; ``can_manage`` lifts the scope to every user's flags.
    The `message_id` filter matches flags whose selection includes that message
    (an `IN`-subquery against the link table, not a join — a join would duplicate
    flags that selected several messages). `search` is a case-insensitive
    substring match on `reason` or `comment` (already `LIKE`-escaped at the edge).

    ``filters.deleted`` swaps the live set for the tombstones still inside the
    restore window, scoped to the caller's own deletes unless ``can_manage``. The
    scope still requires a live parent conversation either way, so a flag hidden
    behind a deleted ancestor never shows up as restorable. ``deleted_cutoff``
    comes from the route, like `get_restorable_flag`'s — reaching for the global
    settings here would let the listing and the restore disagree on the window.
    """
    base = (
        MessageFlag.live_select()
        if not filters.deleted
        else deleted_select(MessageFlag, deleted_cutoff, deleted_by=None if can_manage else caller_id)
    )
    statement = _scope(base, caller_id=caller_id, can_manage=can_manage)
    if filters.conversation_id is not None:
        statement = statement.where(col(MessageFlag.conversation_id) == filters.conversation_id)
    if filters.evaluation_id is not None:
        statement = statement.where(col(MessageFlag.evaluation_id) == filters.evaluation_id)
    if filters.evaluation_group_id is not None:
        statement = statement.where(col(MessageFlag.evaluation_group_id) == filters.evaluation_group_id)
    if filters.scenario_id is not None:
        statement = statement.where(col(MessageFlag.scenario_id) == filters.scenario_id)
    if filters.task_id is not None:
        statement = statement.where(col(MessageFlag.task_id) == filters.task_id)
    if filters.created_by_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == filters.created_by_id)
    if filters.created_from is not None:
        statement = statement.where(col(MessageFlag.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(MessageFlag.created_at) <= filters.created_to)
    if filters.message_id is not None:
        statement = statement.where(
            col(MessageFlag.id).in_(
                select(col(FlaggedMessage.message_flag_id)).where(col(FlaggedMessage.message_id) == filters.message_id)
            )
        )
    if filters.status is not None:
        statement = statement.where(col(MessageFlag.status) == filters.status)
    if filters.red_flagged is not None:
        statement = statement.where(col(MessageFlag.red_flagged) == filters.red_flagged)
    if filters.search is not None:
        pattern = f"%{filters.search}%"
        statement = statement.where(
            or_(
                col(MessageFlag.reason).ilike(pattern, escape="\\"),
                col(MessageFlag.comment).ilike(pattern, escape="\\"),
            ),
        )
    statement = apply_order_by(statement, MessageFlag, order_by).options(
        selectinload(MessageFlag.messages),  # ty: ignore[invalid-argument-type]
        with_live(Message),
    )
    return await paginate(session, statement, limit=limit, offset=offset)


async def flag_counts_by_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    message_ids: Sequence[UUID],
    caller_id: UUID,
    can_manage: bool = False,
) -> dict[UUID, int]:
    """Map each message id to how many of the caller's live flags select it.

    Built for the conversation message-history view: given a page's message ids,
    count the flags **anchored to** ``conversation_id`` (so a flag from another
    conversation can never be counted) that are live and readable by the caller —
    owner-scoped, lifted by ``can_manage`` — the same scope as the flag-read path,
    so the count matches what the flags list returns for `?message_id=`. The
    caller's access to the conversation itself is the route's job. Messages no flag
    selects are absent from the map (the route defaults them to 0).

    Returns:
        ``{message_id: count}``, only for messages with at least one matching flag.
    """
    if not message_ids:
        return {}
    statement = (
        select(col(FlaggedMessage.message_id), func.count())
        .join(MessageFlag, col(MessageFlag.id) == col(FlaggedMessage.message_flag_id))
        .where(
            col(MessageFlag.conversation_id) == conversation_id,
            col(MessageFlag.deleted_at).is_(None),
            col(FlaggedMessage.message_id).in_(message_ids),
        )
        .group_by(col(FlaggedMessage.message_id))
    )
    if not can_manage:
        statement = statement.where(col(MessageFlag.created_by_id) == caller_id)
    rows = (await session.execute(statement)).tuples().all()
    return dict(rows)


async def create_flag(
    session: AsyncSession,
    draft: MessageFlagCreate,
    *,
    caller_id: UUID,
) -> MessageFlag:
    """Flag a selection of ``draft.conversation_id``'s messages, owned by ``caller_id``.

    ``draft`` is the already-validated create payload (conversation, message set,
    and content). Resolves the conversation under the caller's ownership/visibility
    (so flagging one they can't reach is a clean 404, not an opaque FK violation)
    and denormalises its ancestry onto the flag. Every id in ``draft.message_ids``
    must be a live message of the conversation; when ``draft.task_id`` is given, it
    must be a live task of the conversation's scenario — all
    mismatches read as 404.

    A superseded message (one a regenerate/continue pointed past via
    `replaces_message_id`) stays live, so it is flaggable even though the
    conversation read-path surfaces only the survivor — flagging it pins the
    provenance of the exact output a red-teamer found exploit-worthy.

    Raises:
        NotFoundError: If the conversation isn't reachable, a message isn't part of
            it, or ``draft.task_id`` isn't a live task of the conversation's scenario.
    """
    conversation, evaluation_group_id = await resolve_conversation_context(
        session, draft.conversation_id, caller_id=caller_id
    )
    await assert_messages_in_conversation(session, draft.conversation_id, draft.message_ids)
    if draft.task_id is not None:
        task = (
            await session.execute(
                Task.live_select().where(
                    col(Task.id) == draft.task_id,
                    col(Task.scenario_id) == conversation.scenario_id,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise NotFoundError(f"Task {draft.task_id} not found in scenario {conversation.scenario_id}.")
    flag = MessageFlag(
        reason=draft.reason,
        red_flagged=draft.red_flagged,
        comment=draft.comment,
        created_by_id=caller_id,
        conversation_id=conversation.id,
        evaluation_id=conversation.evaluation_id,
        evaluation_group_id=evaluation_group_id,
        scenario_id=conversation.scenario_id,
        task_id=draft.task_id,
    )
    session.add(flag)
    await session.flush()
    session.add_all(FlaggedMessage(message_flag_id=flag.id, message_id=mid) for mid in draft.message_ids)
    await session.flush()
    # Reload server-set timestamps and the just-written selection (the latter forces
    # an eager load of the `messages` projection so the async response doesn't
    # lazy-load it).
    await session.refresh(flag, attribute_names=["created_at", "updated_at", "messages"])
    return flag


async def update_flag(session: AsyncSession, flag: MessageFlag, changes: MessageFlagUpdateChanges) -> MessageFlag:
    """Apply ``changes`` to ``flag`` — writes only fields in `changes.model_fields_set`.

    Content-only (`reason` / `red_flagged` / `comment`): the conversation anchor,
    the selected message set, ancestry, and `status` are not editable here (to
    change the selection, create a new flag). An explicit `null` for `comment`
    clears the note (the only field that allows it).
    """
    for field in changes.model_fields_set:
        setattr(flag, field, getattr(changes, field))
    session.add(flag)
    await session.flush()
    await session.refresh(flag, attribute_names=["updated_at"])
    return flag


async def soft_delete_flag(session: AsyncSession, flag: MessageFlag, *, by_id: UUID) -> MessageFlag:
    """Soft-delete ``flag`` by stamping `deleted_at` (its link rows stay, hidden with it)."""
    flag.soft_delete(by_id)
    session.add(flag)
    await session.flush()
    await session.refresh(flag)
    return flag


async def get_restorable_flag(
    session: AsyncSession,
    flag_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> MessageFlag:
    """Fetch the tombstoned flag ``flag_id`` this caller may restore, with its message set.

    The read scope of `get_flag` over the restorable tombstones: outside the
    window, another author's, another actor's delete, or hidden behind a deleted
    ancestor all read as missing.

    Raises:
        NotFoundError: If no such restorable flag is visible to the caller.
    """
    statement = _scope(
        deleted_select(MessageFlag, deleted_cutoff, deleted_by=None if can_manage else caller_id).where(
            col(MessageFlag.id) == flag_id
        ),
        caller_id=caller_id,
        can_manage=can_manage,
    ).options(selectinload(MessageFlag.messages), with_live(Message))  # ty: ignore[invalid-argument-type]
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = statement.with_for_update(of=MessageFlag).execution_options(populate_existing=True)
    flag = (await session.execute(statement)).scalar_one_or_none()
    if flag is None:
        raise NotFoundError(f"No restorable message flag {flag_id} was deleted within the restore window.")
    return flag


async def restore_flag(session: AsyncSession, flag: MessageFlag) -> MessageFlag:
    """Clear ``flag``'s tombstone (its link rows come back with it)."""
    await restore_row(session, flag, conflict_message="Message flag cannot be restored.")
    await session.refresh(flag, attribute_names=["updated_at", "deleted_at", "deleted_by_id"])
    return flag
