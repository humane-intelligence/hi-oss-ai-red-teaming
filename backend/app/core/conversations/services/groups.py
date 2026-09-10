"""Conversation-group service — the grouping bucket for conversations.

A `ConversationGroup` is the required parent of every conversation (comparing
several models on one prompt is the motivating case, but a group is just a named
bucket). Groups are **owner-scoped** *and* gated by the parent evaluation-group's
visibility — the same rule as conversations (see `services.conversations`): a
caller resolves only the groups they own under a visible, live parent
evaluation-group, with the `evaluation_groups:manage` break-glass (``can_manage``)
lifting the owner and visibility predicates. Parent liveness is enforced regardless.

Creating a group validates the whole reference graph against the evaluation —
the evaluation must be visible, the shared scenario and every model
assignment must belong to it — then inserts the group and one conversation per
entry in a single flush. Deleting a group cascade-soft-deletes its members;
conversely, when a conversation is removed from or moved out of a group, a
now-empty group is pruned (`soft_delete_empty_groups`) — so a live conversation
never dangles under a dead group, and an empty group never lingers.
"""

from collections.abc import Collection
from typing import Any
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.conversations.filters import ConversationGroupFilters
from app.core.conversations.filters import ConversationGroupOrderBy
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.access import groups_granting
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.assignments import get_assignment
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.scenarios import get_scenario
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.soft_delete import with_live


def _scope(
    statement: Select[tuple[ConversationGroup]], *, caller_id: UUID, can_manage: bool, read_any: bool = False
) -> Select[tuple[ConversationGroup]]:
    """Constrain to the caller's own, visible conversation groups (both predicates lifted by ``can_manage``).

    Mirrors `services.conversations._scope`: joins the parent evaluation + group
    for visibility and liveness, then — unless ``can_manage`` — restricts to the
    owner. Parent liveness holds regardless of ``can_manage``. ``read_any`` widens the
    owner predicate to the groups granting the caller `conversations:read_any` in-group,
    so a group owner lists its members' conversation groups; read paths only.
    """
    statement = join_visible_evaluation_group(
        statement, col(ConversationGroup.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )
    if can_manage:
        return statement
    owned = col(ConversationGroup.user_id) == caller_id
    if read_any:
        readable_groups = groups_granting(Permission.CONVERSATIONS_READ_ANY, caller_id)
        return statement.where(or_(owned, col(EvaluationGroup.id).in_(readable_groups)))
    return statement.where(owned)


def _with_conversations(
    statement: Select[tuple[ConversationGroup]],
) -> Select[tuple[ConversationGroup]]:
    """Eager-load the live member conversations for the embedded projection.

    `with_live(Conversation)` is required — the relationship load does not filter
    soft-deleted rows on its own.
    """
    return statement.options(
        selectinload(ConversationGroup.conversations),  # ty: ignore[invalid-argument-type]
        with_live(Conversation),
    )


async def get_conversation_group(
    session: AsyncSession,
    evaluation_id: UUID,
    conversation_group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
    with_conversations: bool = False,
    read_any: bool = False,
) -> ConversationGroup:
    """Fetch one live group ``conversation_group_id`` belonging to ``evaluation_id``.

    The ``evaluation_id`` is part of the lookup, so an id from another evaluation
    resolves to nothing (a clean nested 404). A group owned by another user — or
    one whose parent evaluation-group is no longer visible to the caller — also
    reads as missing unless ``can_manage`` is set. ``for_update`` locks the group
    row for a same-transaction write; ``with_conversations`` embeds its live members.

    Raises:
        NotFoundError: If no live group ``conversation_group_id`` exists in the
            evaluation owned by the caller under a visible, live parent
            evaluation-group (or exists at all, under ``can_manage``).
    """
    statement = _scope(
        ConversationGroup.live_select().where(
            col(ConversationGroup.evaluation_id) == evaluation_id,
            col(ConversationGroup.id) == conversation_group_id,
        ),
        caller_id=caller_id,
        can_manage=can_manage,
        read_any=read_any,
    )
    if with_conversations:
        statement = _with_conversations(statement)
    if for_update:
        # Lock only the group row — the joined evaluation/evaluation-group are
        # visibility predicates, not write targets.
        statement = statement.with_for_update(of=ConversationGroup)
    conversation_group = (await session.execute(statement)).scalar_one_or_none()
    if conversation_group is None:
        raise NotFoundError(f"Conversation group {conversation_group_id} not found in evaluation {evaluation_id}.")
    return conversation_group


async def _count_live_conversations(session: AsyncSession, conversation_group_id: UUID) -> int:
    """Count the live (non-soft-deleted) conversations currently in a group."""
    return (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                col(Conversation.conversation_group_id) == conversation_group_id,
                col(Conversation.deleted_at).is_(None),
            )
        )
    ).scalar_one()


async def acquire_group_for_conversation(
    session: AsyncSession,
    evaluation_id: UUID,
    conversation_group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
) -> ConversationGroup:
    """Resolve and **row-lock** a group to receive an incoming conversation, enforcing the size cap.

    Used by both the conversation-create and the conversation-move paths. The
    `for_update` lock does double duty: it blocks a concurrent group delete from
    racing the insert (which would leave a live conversation dangling under a dead
    group), and it serialises concurrent assignments into the same group so the
    capacity check below cannot be overshot. The cap (`MAX_CONVERSATION_GROUP_SIZE`)
    is structural, so it is enforced regardless of ``can_manage``.

    Raises:
        NotFoundError: If the group is not the caller's own live group under the
            evaluation (via `get_conversation_group`; lifted by ``can_manage``).
        ConflictError: If the group already holds the maximum number of conversations.
    """
    group = await get_conversation_group(
        session, evaluation_id, conversation_group_id, caller_id=caller_id, can_manage=can_manage, for_update=True
    )
    max_size = get_settings().max_conversation_group_size
    if await _count_live_conversations(session, conversation_group_id) >= max_size:
        raise ConflictError(
            f"Conversation group {conversation_group_id} already holds the maximum of {max_size} conversations."
        )
    return group


async def list_conversation_groups(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    read_any: bool = False,
    filters: ConversationGroupFilters,
    order_by: ConversationGroupOrderBy,
    limit: int,
    offset: int,
) -> tuple[list[ConversationGroup], int]:
    """Return one page of the caller's groups (members embedded) matching ``filters``.

    The owner + visibility scope is applied before the user filters so a filter can
    never widen it; ``can_manage`` lifts the scope to every user's groups and
    ``read_any`` widens it to the groups granting the caller `conversations:read_any`.
    """
    statement = _with_conversations(
        _scope(ConversationGroup.live_select(), caller_id=caller_id, can_manage=can_manage, read_any=read_any)
    )
    if filters.evaluation_id is not None:
        statement = statement.where(col(ConversationGroup.evaluation_id) == filters.evaluation_id)
    if filters.scenario_id is not None:
        statement = statement.where(col(ConversationGroup.scenario_id) == filters.scenario_id)
    if filters.user_id is not None:
        statement = statement.where(col(ConversationGroup.user_id) == filters.user_id)
    if filters.created_from is not None:
        statement = statement.where(col(ConversationGroup.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(ConversationGroup.created_at) <= filters.created_to)
    statement = apply_order_by(statement, ConversationGroup, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


class ConversationSpec(NamedTuple):
    """One conversation to create in a batch group — self-documents what would otherwise be a bare tuple."""

    assignment_id: UUID
    parameters: dict[str, Any]
    title: str | None


async def create_conversation_group(
    session: AsyncSession,
    *,
    user_id: UUID,
    evaluation_id: UUID,
    scenario_id: UUID,
    name: str,
    models: list[ConversationSpec],
    can_manage: bool = False,
) -> tuple[ConversationGroup, list[Conversation]]:
    """Create a group plus one conversation per ``models`` entry.

    The same model may appear more than once (each entry becomes its own
    conversation). Validates the whole reference graph against ``evaluation_id`` before
    inserting, so an inconsistent payload fails cleanly rather than as an opaque FK
    violation: the evaluation must be visible to the caller, and the shared scenario
    and every model assignment must belong to it. All resolve to a 404 — addressing a
    resource through an evaluation the caller can't see should not confirm its
    existence.

    The scenario check is vacuous for the create route, which derives
    ``evaluation_id`` from the very scenario it passes on; it stays because the two
    are independent arguments here and `scripts/seed_local.py` calls this directly.

    Args:
        session: Async DB session bound to the request.
        user_id: Owner of the group and its conversations.
        evaluation_id: Evaluation every conversation runs against.
        scenario_id: Shared scenario of the group.
        name: Group display name.
        models: One `ConversationSpec` per conversation; its `parameters` is the
            already-`dump_inference_params`-serialised override dict (the route owns
            that conversion); its `title` is the already-validated per-entry label,
            or ``None``.
        can_manage: Break-glass — lifts the evaluation visibility predicate.

    Returns:
        The created group and its member conversations (creation order).

    Raises:
        NotFoundError: If the evaluation is not visible to the caller, or the
            scenario / any assignment does not belong to the evaluation.
    """
    await get_evaluation(session, evaluation_id, caller_id=user_id, can_manage=can_manage, with_models=False)
    await get_scenario(session, evaluation_id, scenario_id)
    for spec in models:
        await get_assignment(session, evaluation_id, spec.assignment_id)
    licence = await resolve_effective_license(session, evaluation_id)

    conversation_group = ConversationGroup(
        user_id=user_id,
        evaluation_id=evaluation_id,
        scenario_id=scenario_id,
        name=name,
    )
    session.add(conversation_group)
    await session.flush()

    conversations = [
        Conversation(
            user_id=user_id,
            evaluation_id=evaluation_id,
            evaluation_ai_model_id=spec.assignment_id,
            scenario_id=scenario_id,
            conversation_group_id=conversation_group.id,
            title=spec.title,
            parameters=spec.parameters,
            content_protected=licence.protects_conversation_data,
        )
        for spec in models
    ]
    session.add_all(conversations)
    await session.flush()

    await session.refresh(conversation_group, attribute_names=["created_at", "updated_at"])
    for conversation in conversations:
        await session.refresh(conversation, attribute_names=["created_at", "updated_at"])
    return conversation_group, conversations


async def rename_conversation_group(
    session: AsyncSession, conversation_group: ConversationGroup, *, name: str
) -> ConversationGroup:
    """Rename ``conversation_group``. The links and the member set stay immutable."""
    conversation_group.name = name
    session.add(conversation_group)
    await session.flush()
    await session.refresh(conversation_group, attribute_names=["updated_at"])
    return conversation_group


async def soft_delete_conversation_group(
    session: AsyncSession, conversation_group: ConversationGroup, *, by_id: UUID
) -> ConversationGroup:
    """Soft-delete the group **and cascade** to its live member conversations.

    Every conversation belongs to a group, so deleting a group deletes its
    members too — a live conversation never dangles under a dead group. (The
    inverse prune — removing a group emptied by conversation deletes/moves —
    lives in `services.conversations` via `soft_delete_empty_groups`.)
    """
    await session.execute(
        Conversation.live_update()
        .where(col(Conversation.conversation_group_id) == conversation_group.id)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    conversation_group.soft_delete(by_id)
    session.add(conversation_group)
    await session.flush()
    await session.refresh(conversation_group)
    return conversation_group


async def assert_group_has_room(session: AsyncSession, group_id: UUID) -> None:
    """Reject adding another live conversation to a group already at the cap.

    The cap is structural, so every path that makes a conversation live has to
    honour it — restore included, not just create. Counts live members whatever
    the group's own liveness: a restore may be reviving the group in the same
    transaction, so the row is locked directly rather than through
    `get_conversation_group` (which resolves live groups only).

    The lock is what makes the count sound, exactly as in
    `acquire_group_for_conversation`: without it two concurrent restores both read
    a free slot and overshoot the cap, and a group delete can commit between the
    check and the restored row going live — leaving a live conversation under a
    dead group, which the delete's own `live_update` cannot prevent (the row is
    still tombstoned in its snapshot).

    Raises:
        ConflictError: If the group already holds the maximum number of conversations.
    """
    await session.execute(
        select(col(ConversationGroup.id)).where(col(ConversationGroup.id) == group_id).with_for_update()
    )
    max_size = get_settings().max_conversation_group_size
    if await _count_live_conversations(session, group_id) >= max_size:
        raise ConflictError(f"Conversation group {group_id} already holds the maximum of {max_size} conversations.")


async def revive_pruned_group(session: AsyncSession, group_id: UUID) -> None:
    """Clear a group's tombstone if it has one — the inverse of `soft_delete_empty_groups`.

    Invariant repair, not cascade-undo: a conversation's delete may have pruned its
    now-empty group, so restoring that conversation has to bring the group back or
    the row returns parentless. A no-op when the group is live (the usual case: it
    still had other members).

    Unconditional on the window and the deleter here, but only the window holds
    unconditionally:

    * **Window** — the group's tombstone is always newer than the member's, since the
      prune fires during that member's own delete and the group cascade stamps both at
      once. A member inside the window therefore cannot revive a group outside it.
    * **Deleter** — `get_restorable_conversation` has already enforced it on the
      member, which for a group *cascade* means the actor who deleted the group: it
      stamps every member that was live. It does **not** extend to a member tombstoned
      *before* the group delete — `live_update` skips it, so it keeps its own deleter
      and its owner can revive a group someone else deleted while holding only
      `conversations:delete` (pinned by
      `test_restore_revives_a_group_someone_else_deleted_when_the_member_predates_it`).
      Accepted: the alternative is a live conversation under a dead group, the exact
      invariant this repair exists to prevent.
    """
    await session.execute(
        update(ConversationGroup)
        .where(col(ConversationGroup.id) == group_id, col(ConversationGroup.deleted_at).is_not(None))
        .values(deleted_at=None, deleted_by_id=None)
    )


async def soft_delete_empty_groups(session: AsyncSession, group_ids: Collection[UUID], *, by_id: UUID) -> None:
    """Soft-delete any group in ``group_ids`` that has no live conversation left.

    The invariant-keeper for the inverse of the delete cascade: when a conversation
    is removed from or moved out of a group (single or bulk), the now-empty group
    is cleaned up. A no-op for ``group_ids`` that still hold a live member, and for
    an empty collection.
    """
    if not group_ids:
        return
    has_live_conversation = (
        select(literal(1))
        .where(
            col(Conversation.conversation_group_id) == col(ConversationGroup.id),
            col(Conversation.deleted_at).is_(None),
        )
        .exists()
    )
    await session.execute(
        ConversationGroup.live_update()
        .where(col(ConversationGroup.id).in_(group_ids), ~has_live_conversation)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
