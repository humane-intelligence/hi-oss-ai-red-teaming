"""Note service — pure async functions over an `AsyncSession`.

Notes are **owner-scoped on read** (a caller sees only what they authored,
unless the `evaluation_groups:manage` break-glass lifts it) but deliberately **not
owner-scoped on create**: authoring requires only that the conversation's group is
visible to the caller, so an annotator can leave a note on a red-teamer's transcript.
The flag sibling (`services/message_flags.resolve_conversation_context`) keeps the
ownership predicate instead.

The ancestry is denormalised onto the row at create time (resolved once under the
caller's visibility so it can't drift), so reads need only the note's own
columns; the selected messages come from the viewonly `Note.messages`
relationship (`selectinload` + `with_live(Message)`).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.annotations.filters import NoteFilters
from app.core.annotations.filters import NoteOrderBy
from app.core.annotations.models import Note
from app.core.annotations.models import NotedMessage
from app.core.annotations.schemas import NoteCreate
from app.core.annotations.schemas import NoteUpdateChanges
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.services.messages import assert_messages_in_conversation
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.access import join_conversation_scoped
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


def _scope(statement: Select[tuple[Note]], *, caller_id: UUID, can_manage: bool) -> Select[tuple[Note]]:
    """Constrain to the caller's own notes on a live, group-visible conversation.

    The shared `join_conversation_scoped` scope, applied before any user filter — and
    the paginated `total` counts through this same statement, so a row under a
    soft-deleted parent is absent from the page *and* uncounted.
    """
    return join_conversation_scoped(statement, Note, caller_id=caller_id, can_manage=can_manage)


async def resolve_note_conversation(
    session: AsyncSession, conversation_id: UUID, *, caller_id: UUID
) -> tuple[Conversation, UUID]:
    """Resolve a conversation a note may attach to, and its evaluation-group id.

    Walks `conversation → evaluation → group`, all live, under the group's
    visibility **only** — there is no `Conversation.user_id == caller_id`
    predicate, unlike the flag path: the route owns the permission gate, and what
    this resolver adds to it is visibility alone. A conversation in a group the
    caller cannot see reads as missing — addressing it should not confirm its
    existence.

    Takes no ``can_manage``: the `evaluation_groups:manage` break-glass lifts the
    read predicates but **not** this one, so a manager who cannot see the group
    cannot author a note in it either.

    Returns:
        The conversation (source of the denormalised `evaluation_id`) and its
        evaluation-group id, which the conversation row does not carry.

    Raises:
        NotFoundError: If no live conversation ``conversation_id`` exists under a
            live evaluation and a live group visible to the caller.
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
    )
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    conversation, evaluation_group_id = row
    return conversation, evaluation_group_id


async def create_note(session: AsyncSession, draft: NoteCreate, *, caller_id: UUID) -> Note:
    """Attach a note to a selection of ``draft.conversation_id``'s messages, by ``caller_id``.

    Resolves the conversation under the group's visibility (so noting one the
    caller cannot reach is a clean 404, not an opaque FK violation) and
    denormalises its ancestry onto the note. Every id in
    ``draft.message_ids`` must be a live message of that conversation.

    A superseded message (one a regenerate/continue pointed past via
    `replaces_message_id`) stays live and can still be noted — the note is about
    the output that was produced, which supersession does not undo.

    Raises:
        NotFoundError: If the conversation isn't reachable or an id in
            ``draft.message_ids`` isn't a live message of it.
    """
    conversation, evaluation_group_id = await resolve_note_conversation(
        session, draft.conversation_id, caller_id=caller_id
    )
    await assert_messages_in_conversation(session, draft.conversation_id, draft.message_ids)
    note = Note(
        text=draft.text,
        created_by_id=caller_id,
        conversation_id=conversation.id,
        evaluation_id=conversation.evaluation_id,
        evaluation_group_id=evaluation_group_id,
    )
    session.add(note)
    await session.flush()
    session.add_all(NotedMessage(note_id=note.id, message_id=message_id) for message_id in draft.message_ids)
    await session.flush()
    # Reload the server-set timestamps and the just-written selection — the latter
    # forces an eager load so the async response never lazy-loads `messages`.
    await session.refresh(note, attribute_names=["created_at", "updated_at", "messages"])
    return note


async def get_note(
    session: AsyncSession,
    note_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
) -> Note:
    """Fetch one live note ``note_id`` readable by the caller, with its message set.

    A note authored by another user — or whose parent group is no longer
    visible to the caller, or whose conversation / evaluation / group is
    soft-deleted — reads as missing (404, no existence leak) unless ``can_manage``
    lifts the author / visibility predicates.

    Mutation paths pass ``for_update=True``. That load also sets
    `populate_existing`, so an instance an earlier unlocked read left in the
    identity map is overwritten with the locked state rather than handed back
    stale — otherwise a guard or an audit `before` snapshot would read pre-lock
    values and the row lock would protect nothing.

    Raises:
        NotFoundError: If no such note is readable by the caller.
    """
    statement = _scope(
        Note.live_select().where(col(Note.id) == note_id),
        caller_id=caller_id,
        can_manage=can_manage,
    ).options(selectinload(Note.messages), with_live(Message))  # ty: ignore[invalid-argument-type]
    if for_update:
        # Lock only the note row — the joined ancestry are visibility predicates.
        # `populate_existing` refreshes a cached instance from the locked read.
        statement = statement.with_for_update(of=Note).execution_options(populate_existing=True)
    note = (await session.execute(statement)).scalar_one_or_none()
    if note is None:
        raise NotFoundError(f"Note {note_id} not found.")
    return note


async def list_notes(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    filters: NoteFilters,
    order_by: NoteOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Note], int]:
    """Return one page of the caller's notes matching ``filters``, each with its message set.

    The author + group-visibility scope is applied *before* the user filters so a
    filter can never widen it; ``can_manage`` lifts the scope to every author's
    notes. The `message_id` filter matches notes whose selection
    includes that message, as an `IN`-subquery against the link table: for a single id a
    join would return the same rows, but the subquery keeps the result one row per note
    regardless of link-table cardinality, so widening the filter to several ids cannot
    start duplicating. `search` is a case-insensitive substring
    match on `text` (already `LIKE`-escaped at the edge).

    ``filters.deleted`` swaps the live set for the tombstones still inside the
    restore window, scoped to the caller's own deletes unless ``can_manage``.
    Ancestor liveness still applies, so a note behind a deleted conversation never
    reads as restorable. ``deleted_cutoff`` comes from the
    route, like `get_restorable_note`'s — reaching for the global settings
    here would let the listing and the restore disagree on the window.
    """
    base = (
        Note.live_select()
        if not filters.deleted
        else deleted_select(Note, deleted_cutoff, deleted_by=None if can_manage else caller_id)
    )
    statement = _scope(base, caller_id=caller_id, can_manage=can_manage)
    if filters.conversation_id is not None:
        statement = statement.where(col(Note.conversation_id) == filters.conversation_id)
    if filters.evaluation_id is not None:
        statement = statement.where(col(Note.evaluation_id) == filters.evaluation_id)
    if filters.evaluation_group_id is not None:
        statement = statement.where(col(Note.evaluation_group_id) == filters.evaluation_group_id)
    if filters.created_by_id is not None:
        statement = statement.where(col(Note.created_by_id) == filters.created_by_id)
    if filters.created_from is not None:
        statement = statement.where(col(Note.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(Note.created_at) <= filters.created_to)
    if filters.message_id is not None:
        statement = statement.where(
            col(Note.id).in_(
                select(col(NotedMessage.note_id)).where(col(NotedMessage.message_id) == filters.message_id)
            )
        )
    if filters.search is not None:
        statement = statement.where(col(Note.text).ilike(f"%{filters.search}%", escape="\\"))
    statement = apply_order_by(statement, Note, order_by).options(
        selectinload(Note.messages),  # ty: ignore[invalid-argument-type]
        with_live(Message),
    )
    return await paginate(session, statement, limit=limit, offset=offset)


async def update_note(session: AsyncSession, note: Note, changes: NoteUpdateChanges) -> Note:
    """Apply ``changes`` to ``note`` — writes only fields in `changes.model_fields_set`.

    Content-only (`text`): the conversation anchor, the selected message set and the
    ancestry are not editable here — a different selection is a new note.
    ``note`` is expected to come from `get_note(..., for_update=True)`,
    which is also what makes an audit `before` snapshot taken from it trustworthy.
    """
    for field in changes.model_fields_set:
        setattr(note, field, getattr(changes, field))
    session.add(note)
    await session.flush()
    await session.refresh(note, attribute_names=["updated_at"])
    return note


async def soft_delete_note(session: AsyncSession, note: Note, *, by_id: UUID) -> Note:
    """Soft-delete ``note`` by stamping `deleted_at` (its link rows stay, hidden with it).

    Refreshes by name, like `create_note`: a bare `refresh()` expires the
    eagerly loaded `messages` too, and the next read of it would lazy-load under
    asyncio (`MissingGreenlet`).
    """
    note.soft_delete(by_id)
    session.add(note)
    await session.flush()
    await session.refresh(note, attribute_names=["updated_at", "deleted_at"])
    return note


async def get_restorable_note(
    session: AsyncSession,
    note_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Note:
    """Fetch the tombstoned note ``note_id`` this caller may restore, with its selection.

    The read scope of `get_note` over the restorable tombstones: outside the
    window, another author's, another actor's delete, or hidden behind a deleted
    ancestor all read as missing.

    Raises:
        NotFoundError: If no such restorable note is readable by the caller.
    """
    statement = (
        _scope(
            deleted_select(Note, deleted_cutoff, deleted_by=None if can_manage else caller_id).where(
                col(Note.id) == note_id
            ),
            caller_id=caller_id,
            can_manage=can_manage,
        )
        .options(selectinload(Note.messages), with_live(Message))  # ty: ignore[invalid-argument-type]
        .with_for_update(of=Note)
        .execution_options(populate_existing=True)
    )
    note = (await session.execute(statement)).scalar_one_or_none()
    if note is None:
        raise NotFoundError(f"No restorable note {note_id} was deleted within the restore window.")
    return note


async def restore_note(session: AsyncSession, note: Note) -> Note:
    """Clear ``note``'s tombstone (its link rows come back with it)."""
    await restore_row(session, note, conflict_message="Note cannot be restored.")
    await session.refresh(note, attribute_names=["updated_at", "deleted_at", "deleted_by_id"])
    return note
