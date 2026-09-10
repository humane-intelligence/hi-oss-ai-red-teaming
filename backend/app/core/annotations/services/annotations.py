"""Annotation service — pure async functions over an `AsyncSession`.

Annotations are the module's deliberately **shared-read** entity: any caller who can see
the group sees every annotation in it (a tag exists to be aggregated), so reads pass
``author_scoped=False`` to the one authorization home instead of forking a private scope.
Deletes stay author-scoped — and because the read scope is wider than the write scope,
a foreign annotation reads fine and refuses deletion with a 403, not a 404: its
existence is already public to the caller, so there is nothing to avoid leaking.
Authoring follows the note path (group visibility only, no ownership predicate, no
break-glass): an annotator labels a red-teamer's message.

Every annotation references an `AnnotationLabel` — curated, or one the author typed, which
`find_or_create_user_label` resolves before the row is written. The label relationship is
loaded **without** a liveness filter, on purpose: a retired (soft-deleted) label leaves the
picker but keeps rendering on the rows that reference it.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.annotations.filters import AnnotationFilters
from app.core.annotations.filters import AnnotationOrderBy
from app.core.annotations.models import Annotation
from app.core.annotations.models import AnnotationLabel
from app.core.annotations.schemas import AnnotationCreate
from app.core.annotations.services.annotation_labels import find_or_create_user_label
from app.core.annotations.services.annotation_labels import labels_used_in_conversation
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.access import join_conversation_scoped
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import BadRequestError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row


def _scope(statement: Select[tuple[Annotation]], *, caller_id: UUID, can_manage: bool) -> Select[tuple[Annotation]]:
    """Constrain to annotations on a live, group-visible conversation — every author's.

    The shared `join_conversation_scoped` scope with the author predicate dropped
    (`author_scoped=False`) — the entity's one deliberate divergence, documented there.
    """
    return join_conversation_scoped(
        statement, Annotation, caller_id=caller_id, can_manage=can_manage, author_scoped=False
    )


def _with_label(statement: Select[tuple[Annotation]]) -> Select[tuple[Annotation]]:
    # Deliberately no `with_live(AnnotationLabel)`: a retired label must keep rendering
    # on the rows that reference it — only the picker filters liveness.
    return statement.options(selectinload(Annotation.label))  # ty: ignore[invalid-argument-type]


async def resolve_annotation_message(
    session: AsyncSession, message_id: UUID, *, caller_id: UUID
) -> tuple[Conversation, UUID]:
    """Resolve the conversation of a message an annotation may attach to, plus its group id.

    Walks `message → turn → conversation → evaluation → group`, all live, under the
    group's visibility **only** — the note path, not the flag path: there is no
    `Conversation.user_id == caller_id` predicate, so an annotator can label a
    red-teamer's message. A message the caller cannot reach reads as missing —
    addressing it should not confirm it exists.

    Takes no ``can_manage``: the break-glass lifts read predicates, not authoring.
    A superseded-but-live message still qualifies — the label is about the output
    that was produced, which supersession does not undo.

    Raises:
        NotFoundError: If no live message ``message_id`` exists under a live
            conversation, evaluation and group visible to the caller.
    """
    statement = (
        select(Conversation, col(EvaluationGroup.id))
        .select_from(Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .join(Conversation, col(Turn.conversation_id) == col(Conversation.id))
        .join(Evaluation, col(Conversation.evaluation_id) == col(Evaluation.id))
        .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(Message.id) == message_id)
        # Message/turn liveness is belt-and-braces — nothing soft-deletes either today — but
        # this is the authoring authorization path, and `assert_messages_in_conversation`
        # (the flag/note sibling) filters both, so the two must not diverge.
        .where(col(Message.deleted_at).is_(None))
        .where(col(Turn.deleted_at).is_(None))
        .where(col(Conversation.deleted_at).is_(None))
        .where(col(Evaluation.deleted_at).is_(None))
        .where(col(EvaluationGroup.deleted_at).is_(None))
        .where(group_visible_to(caller_id))
    )
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(f"Message {message_id} not found.")
    conversation, evaluation_group_id = row
    return conversation, evaluation_group_id


async def _get_live_duplicate(
    session: AsyncSession, message_id: UUID, label_id: UUID, *, caller_id: UUID
) -> Annotation | None:
    """The caller's live annotation carrying ``label_id`` on ``message_id``, if any.

    One key now, not two: case folding moved onto the label row, so a typed label is already
    resolved to a single id before it reaches here.
    """
    statement = Annotation.live_select().where(
        col(Annotation.message_id) == message_id,
        col(Annotation.label_id) == label_id,
        col(Annotation.created_by_id) == caller_id,
    )
    return (await session.execute(_with_label(statement))).scalar_one_or_none()


async def _resolve_label(
    session: AsyncSession, label_id: UUID, *, conversation_id: UUID, caller_id: UUID
) -> AnnotationLabel:
    """The label ``label_id`` names, as a row this caller may attach.

    Curated and the caller's own resolve to themselves. A **colleague's** label is a special
    case rather than a refusal: the picker offers one when it is already used on this
    conversation (that is the convergence rule), so refusing it here would offer something the
    create path rejects. It resolves instead to the caller's *own* row of the same wording —
    which is what "a label you type is saved as yours" means.

    `ix_annotations_message_label_author` keys on `label_id`, so it cannot see that two rows
    share a name — uniqueness by wording is established upstream instead, by
    `find_or_create_user_label` resolving a wording to the curated row, then the caller's own,
    before it mints anything. The picker ranks the same way, and the states that could still split
    a wording across two ids are handled where they arise rather than per message: a curated entry
    reworded onto a name someone typed first is refused by `sync_annotation_labels` (save across
    its own scan/upsert window, see `_precedence`), and two catalog entries agreeing up to case by
    `test_annotation_label_catalog`.

    Consequence worth naming: two annotators converge on the *spelling*, not on one row, so
    aggregating a custom label across annotators is a group-by on name. The curated vocabulary
    is what aggregates by id.

    Anything else — unknown, retired, or a colleague's label not used on this conversation —
    reads as missing: it was never offered, so confirming it exists is not this caller's due.

    Raises:
        NotFoundError: If no such label is attachable by this caller.
    """
    label = (
        await session.execute(AnnotationLabel.live_select().where(col(AnnotationLabel.id) == label_id))
    ).scalar_one_or_none()
    if label is None:
        raise NotFoundError(f"Annotation label {label_id} not found.")
    if label.created_by_id is None or label.created_by_id == caller_id:
        return label

    offered_here = await session.execute(
        labels_used_in_conversation(conversation_id, caller_id=caller_id, can_manage=False).where(
            col(Annotation.label_id) == label_id
        )
    )
    if offered_here.first() is None:
        raise NotFoundError(f"Annotation label {label_id} not found.")
    return await find_or_create_user_label(session, label.name, caller_id=caller_id)


async def create_annotation(
    session: AsyncSession, draft: AnnotationCreate, *, caller_id: UUID
) -> tuple[Annotation, bool]:
    """Label ``draft.message_id`` as ``caller_id``, idempotently.

    Returns ``(annotation, created)``. The flag is what keeps the audit trail honest: a
    repeat returns the existing row, and recording a second `annotation.create` for it
    would put a false entry on an admin-visible surface.

    Resolves the message under group visibility (an unreachable one is a clean 404) and
    denormalises the ancestry onto the row.

    ``draft.label_id`` is resolved by `_resolve_label`: curated and the caller's own pass
    through, a colleague's label already used on this conversation becomes the caller's own row
    of the same wording, anything else is a 404. ``draft.text`` instead *names* a label: the
    curated row of that wording if one exists, else the caller's own, else a new one scoped to
    them.

    Re-annotating the same message with the same label returns the existing row rather than
    409 — the same intent, the completions precedent. The `IntegrityError` arm covers the
    insert race; because the author is part of the partial unique, the racing insert is
    necessarily this same caller.

    Raises:
        NotFoundError: If the message isn't reachable, or `label_id` names no usable label.
    """
    conversation, evaluation_group_id = await resolve_annotation_message(session, draft.message_id, caller_id=caller_id)
    if draft.label_id is not None:
        label = await _resolve_label(session, draft.label_id, conversation_id=conversation.id, caller_id=caller_id)
    elif draft.text is not None:
        label = await find_or_create_user_label(session, draft.text, caller_id=caller_id)
    else:
        # Unreachable over HTTP — the payload's exactly-one validator refuses it as a 422 —
        # but this is a service function, so a direct caller gets a real error, not a crash.
        raise BadRequestError("Provide exactly one of 'label_id' and 'text'.")

    existing = await _get_live_duplicate(session, draft.message_id, label.id, caller_id=caller_id)
    if existing is not None:
        return existing, False

    annotation = Annotation(
        message_id=draft.message_id,
        label_id=label.id,
        created_by_id=caller_id,
        conversation_id=conversation.id,
        evaluation_id=conversation.evaluation_id,
        evaluation_group_id=evaluation_group_id,
    )
    try:
        async with session.begin_nested():
            session.add(annotation)
            await session.flush()
    except IntegrityError:
        won = await _get_live_duplicate(session, draft.message_id, label.id, caller_id=caller_id)
        if won is None:
            raise
        return won, False
    # Reload the server-set timestamps and the label — the eager load keeps the async
    # response from lazy-loading `label`.
    await session.refresh(annotation, attribute_names=["created_at", "updated_at", "label"])
    return annotation, True


async def get_annotation(
    session: AsyncSession,
    annotation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
) -> Annotation:
    """Fetch one live annotation ``annotation_id`` visible to the caller, with its label.

    Any author's — reads are shared. One whose parent group is not visible, or whose
    conversation / evaluation / group is soft-deleted, reads as missing (404).

    Mutation paths pass ``for_update=True``; that load also sets `populate_existing`,
    so a cached instance is overwritten with the locked state.

    Raises:
        NotFoundError: If no such annotation is visible to the caller.
    """
    statement = _with_label(
        _scope(
            Annotation.live_select().where(col(Annotation.id) == annotation_id),
            caller_id=caller_id,
            can_manage=can_manage,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=Annotation).execution_options(populate_existing=True)
    annotation = (await session.execute(statement)).scalar_one_or_none()
    if annotation is None:
        raise NotFoundError(f"Annotation {annotation_id} not found.")
    return annotation


async def list_annotations(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    filters: AnnotationFilters,
    order_by: AnnotationOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Annotation], int]:
    """Return one page of the annotations visible to the caller, each with its label.

    The group-visibility scope is applied *before* the user filters, so a filter can
    never widen it; there is no author scope to lift (`created_by_id` is an ordinary
    filter here). A superseded-but-live message's annotations keep listing — the
    transcript no longer renders the message, the aggregation still counts it.

    ``filters.deleted`` swaps the live set for the tombstones still inside the restore
    window, scoped to the caller's **own deletes** unless ``can_manage`` — the restore
    surface stays personal even though reads are shared, matching what the restore
    accepts. ``deleted_cutoff`` comes from the route, like `get_restorable_annotation`'s.
    """
    base = (
        Annotation.live_select()
        if not filters.deleted
        else deleted_select(Annotation, deleted_cutoff, deleted_by=None if can_manage else caller_id)
    )
    statement = _scope(base, caller_id=caller_id, can_manage=can_manage)
    if filters.message_id is not None:
        statement = statement.where(col(Annotation.message_id) == filters.message_id)
    if filters.conversation_id is not None:
        statement = statement.where(col(Annotation.conversation_id) == filters.conversation_id)
    if filters.evaluation_id is not None:
        statement = statement.where(col(Annotation.evaluation_id) == filters.evaluation_id)
    if filters.evaluation_group_id is not None:
        statement = statement.where(col(Annotation.evaluation_group_id) == filters.evaluation_group_id)
    if filters.label_id is not None:
        statement = statement.where(col(Annotation.label_id) == filters.label_id)
    if filters.created_by_id is not None:
        statement = statement.where(col(Annotation.created_by_id) == filters.created_by_id)
    if filters.created_from is not None:
        statement = statement.where(col(Annotation.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(Annotation.created_at) <= filters.created_to)
    statement = _with_label(apply_order_by(statement, Annotation, order_by))
    return await paginate(session, statement, limit=limit, offset=offset)


def assert_may_delete_annotation(annotation: Annotation, *, caller_id: UUID, can_manage: bool) -> None:
    """Refuse deleting another author's annotation without the break-glass.

    A 403, not a 404: the read scope is wider than the write scope, so the row's
    existence is already public to this caller — unlike notes, where the scopes
    coincide and a foreign row reads as missing.

    Raises:
        ForbiddenError: If the caller is neither the author nor a manager.
    """
    if not can_manage and annotation.created_by_id != caller_id:
        raise ForbiddenError("Only the annotation's author may delete it.")


async def soft_delete_annotation(session: AsyncSession, annotation: Annotation, *, by_id: UUID) -> Annotation:
    """Soft-delete ``annotation`` by stamping `deleted_at`, freeing its dedup slot.

    Refreshes by name: a bare `refresh()` expires the eagerly loaded `label` too, and
    the next read of it would lazy-load under asyncio (`MissingGreenlet`).
    """
    annotation.soft_delete(by_id)
    session.add(annotation)
    await session.flush()
    await session.refresh(annotation, attribute_names=["updated_at", "deleted_at"])
    return annotation


async def get_restorable_annotation(
    session: AsyncSession,
    annotation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Annotation:
    """Fetch the tombstoned annotation ``annotation_id`` this caller may restore.

    Outside the window, another actor's delete, or hidden behind a deleted ancestor all
    read as missing. Restoring can also collide with a duplicate created since the
    delete — the partial unique refuses it, surfaced by `restore_annotation`.

    Raises:
        NotFoundError: If no such restorable annotation exists for the caller.
    """
    statement = (
        _with_label(
            _scope(
                deleted_select(Annotation, deleted_cutoff, deleted_by=None if can_manage else caller_id).where(
                    col(Annotation.id) == annotation_id
                ),
                caller_id=caller_id,
                can_manage=can_manage,
            )
        )
        .with_for_update(of=Annotation)
        .execution_options(populate_existing=True)
    )
    annotation = (await session.execute(statement)).scalar_one_or_none()
    if annotation is None:
        raise NotFoundError(f"No restorable annotation {annotation_id} was deleted within the restore window.")
    return annotation


async def restore_annotation(session: AsyncSession, annotation: Annotation) -> Annotation:
    """Clear ``annotation``'s tombstone; a duplicate created since the delete reads as 409."""
    await restore_row(session, annotation, conflict_message="An identical annotation already exists.")
    await session.refresh(annotation, attribute_names=["updated_at", "deleted_at", "deleted_by_id"])
    return annotation
