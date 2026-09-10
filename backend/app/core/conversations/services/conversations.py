"""Conversation service — pure async functions over an `AsyncSession`.

Conversations are **owner-scoped** *and* gated by the parent group's visibility:
a caller reads, updates, and deletes only conversations they own (`user_id ==
caller_id`) whose parent evaluation's group is still visible to them (`public`, or
one they hold an object role on). So a red-teamer unassigned from a private group
loses sight of its evaluations — and their conversations against those evaluations
become unavailable — exactly as the create path already required. The
`evaluation_groups:manage` break-glass (``can_manage``) lifts both predicates so an
admin resolves any user's conversation; parent **liveness** is enforced regardless.

Routers stay thin and raise `APIError` subclasses; soft-deleted rows are filtered
per-statement via the `live_*` factories on `BaseModel`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.inference_params import merge_inference_params
from app.core.ai_gateway.models import AiModel
from app.core.auth.roles import Permission
from app.core.conversations.filters import ConversationFilters
from app.core.conversations.filters import ConversationOrderBy
from app.core.conversations.models import Conversation
from app.core.conversations.services.groups import acquire_group_for_conversation
from app.core.conversations.services.groups import assert_group_has_room
from app.core.conversations.services.groups import revive_pruned_group
from app.core.conversations.services.groups import soft_delete_empty_groups
from app.core.evaluations.access import groups_granting
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.assignments import get_assignment
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.scenarios import get_scenario
from app.core.evaluations.services.tag_keys import assert_tags_allowed
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row


def _scope(
    statement: Select[tuple[Conversation]], *, caller_id: UUID, can_manage: bool, read_any: bool = False
) -> Select[tuple[Conversation]]:
    """Constrain to the caller's own, group-visible conversations.

    Two predicates, both lifted by ``can_manage``:

    * **owner** — `user_id == caller_id`.
    * **group visibility** — `group_visible_to(caller_id)` (the parent group is
      `public` or one the caller holds a live object role on). A member unassigned
      from a private group fails this, so their conversations against its
      evaluations vanish from reads/writes — the same rule the create path applies.

    ``read_any`` widens the *owner* predicate (never visibility) to also admit
    conversations whose parent group grants the caller `conversations:read_any` in-group,
    so a group owner reads its members' transcripts. Only read paths pass it: mutations
    call this without it and stay owner-scoped for every holder.

    Parent evaluation- and group-**liveness** is joined and enforced regardless of
    ``can_manage`` (a soft-deleted parent hides its conversations from everyone,
    managers included); ``can_manage`` lifts only the owner and visibility predicates.
    """
    statement = join_visible_evaluation_group(
        statement, col(Conversation.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )
    if can_manage:
        return statement
    owned = col(Conversation.user_id) == caller_id
    if read_any:
        readable_groups = groups_granting(Permission.CONVERSATIONS_READ_ANY, caller_id)
        return statement.where(or_(owned, col(EvaluationGroup.id).in_(readable_groups)))
    return statement.where(owned)


def _join_live_assignment(statement: Select[tuple[Conversation]]) -> Select[tuple[Conversation]]:
    """Require the conversation's model assignment to still be live.

    Only the tombstone reads need this. A *live* conversation always has a live
    assignment, because removing one cascades to its conversations
    (`soft_delete_conversations_for_assignment`) — which is also what makes the
    cascade's tombstones look restorable: it records the unassigning admin as the
    deleter. Restoring one would hand back a conversation that cannot run, since
    its chosen model is gone, so a cascade tombstone reads as missing here.
    """
    return statement.join(
        EvaluationAiModel, col(Conversation.evaluation_ai_model_id) == col(EvaluationAiModel.id)
    ).where(col(EvaluationAiModel.deleted_at).is_(None))


async def get_conversation(
    session: AsyncSession,
    evaluation_id: UUID,
    conversation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
    read_any: bool = False,
) -> Conversation:
    """Fetch one live conversation ``conversation_id`` belonging to ``evaluation_id``.

    The ``evaluation_id`` is part of the lookup, so an id from another evaluation
    resolves to nothing (a clean nested 404, not a cross-evaluation read). A
    conversation owned by another user — or one whose parent group is no longer
    visible to the caller (e.g. after being unassigned from a private group) —
    also reads as missing (404, no existence leak) unless ``can_manage`` is set.
    Mutation paths pass ``for_update=True`` to lock the row for a same-transaction
    write.

    Raises:
        NotFoundError: If no live conversation ``conversation_id`` exists in the
            evaluation owned by the caller under a visible, live parent group (or
            exists at all, under ``can_manage``).
    """
    statement = _scope(
        Conversation.live_select().where(
            col(Conversation.evaluation_id) == evaluation_id,
            col(Conversation.id) == conversation_id,
        ),
        caller_id=caller_id,
        can_manage=can_manage,
        read_any=read_any,
    )
    if for_update:
        # Lock only the conversation row — the joined evaluation/group are
        # visibility predicates, not write targets.
        statement = statement.with_for_update(of=Conversation)
    conversation = (await session.execute(statement)).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError(f"Conversation {conversation_id} not found in evaluation {evaluation_id}.")
    return conversation


async def resolve_dispatch_target(
    session: AsyncSession, conversation: Conversation
) -> tuple[str, dict[str, Any], bool]:
    """Resolve ``(model_alias, effective inference params, mask)`` to drive generation.

    Params follow the cascade model → assignment → conversation (`merge_inference_params`).
    ``mask`` is the evaluation's `mask_models_enabled` — when set, the streamed error
    detail (which can name the real model) must be masked.
    """
    row = (
        await session.execute(
            select(
                col(AiModel.model_alias),
                col(AiModel.parameters),
                col(EvaluationAiModel.parameters),
                col(Evaluation.mask_models_enabled),
            )
            .join(EvaluationAiModel, col(AiModel.id) == col(EvaluationAiModel.model_id))
            .join(Evaluation, col(Evaluation.id) == col(EvaluationAiModel.evaluation_id))
            .where(col(EvaluationAiModel.id) == conversation.evaluation_ai_model_id)
        )
    ).first()
    # A live conversation should always resolve its assignment (the model-unassign
    # cascade soft-deletes orphaned conversations). Guard defensively anyway: a clean
    # 404 beats a raw NoResultFound 500.
    if row is None:
        raise NotFoundError(f"Model assignment for conversation {conversation.id} is unavailable.")
    alias, model_params, assignment_params, mask = row
    params = merge_inference_params(model_params, assignment_params, conversation.parameters)
    return alias, params, mask


async def list_conversations(  # noqa: PLR0913 — keyword-only args mirror the listing's scope, page and window; a carrier object would just shift the surface area
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    read_any: bool = False,
    filters: ConversationFilters,
    order_by: ConversationOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Conversation], int]:
    """Return one page of the caller's conversations matching ``filters``.

    The owner + group-visibility scope is applied *before* the user filters so a
    filter can never widen it; ``can_manage`` lifts the scope to every user's
    conversations, and ``read_any`` widens it to the groups granting the caller
    `conversations:read_any`.

    ``filters.deleted`` swaps the live set for the tombstones still inside the
    restore window, scoped to the caller's own deletes unless ``can_manage``.
    Parent evaluation/group liveness still applies, so a conversation behind a
    deleted parent is not restorable, and neither is one the model-unassign
    cascade tombstoned (`_join_live_assignment`) — the listing is the restore
    surface, so it must not offer rows `restore_conversation` would refuse.
    ``deleted_cutoff`` comes from the route, like `get_restorable_conversation`'s
    — reaching for the global settings here would let the listing and the restore
    disagree on the window.

    ``read_any`` is not passed on the deleted branch, where it would be inert: the
    deleter predicate binds first, and a non-manager can only delete a conversation they
    own (delete resolves it owner-scoped), so `deleted_by_id == caller_id` already implies
    ownership. Widening the owner predicate alongside it could admit nothing further —
    which is also why the tombstone view needs no `read_any` test of its own.
    """
    if not filters.deleted:
        statement = _scope(Conversation.live_select(), caller_id=caller_id, can_manage=can_manage, read_any=read_any)
    else:
        statement = _join_live_assignment(
            _scope(
                deleted_select(Conversation, deleted_cutoff, deleted_by=None if can_manage else caller_id),
                caller_id=caller_id,
                can_manage=can_manage,
            )
        )
    if filters.evaluation_id is not None:
        statement = statement.where(col(Conversation.evaluation_id) == filters.evaluation_id)
    if filters.scenario_id is not None:
        statement = statement.where(col(Conversation.scenario_id) == filters.scenario_id)
    if filters.conversation_group_id is not None:
        statement = statement.where(col(Conversation.conversation_group_id) == filters.conversation_group_id)
    if filters.title is not None:
        statement = statement.where(col(Conversation.title).ilike(f"%{filters.title}%", escape="\\"))
    if filters.user_id is not None:
        statement = statement.where(col(Conversation.user_id) == filters.user_id)
    if filters.created_from is not None:
        statement = statement.where(col(Conversation.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(Conversation.created_at) <= filters.created_to)
    statement = apply_order_by(statement, Conversation, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def create_conversation(  # noqa: PLR0913 — keyword-only args mirror the conversation's column set; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    user_id: UUID,
    evaluation_id: UUID,
    evaluation_ai_model_id: UUID,
    scenario_id: UUID,
    parameters: dict[str, Any],
    conversation_group_id: UUID,
    title: str | None = None,
    tags: dict[str, str] | None = None,
    can_manage: bool = False,
) -> Conversation:
    """Create an empty conversation owned by ``user_id`` inside ``conversation_group_id``.

    Validates the whole reference graph against ``evaluation_id`` before
    inserting, so an inconsistent payload fails cleanly rather than as an opaque
    FK violation: the evaluation must be visible to the caller, the model
    assignment and the scenario must belong to it, and the group must be the
    caller's own live group under the same evaluation — every conversation
    belongs to a group. All resolve to a 404 — addressing a resource through an
    evaluation the caller can't see should not confirm its existence. The
    scenario must additionally **match the group's** (a group's members all share
    its scenario) — a mismatch between two otherwise-valid resources is a 409,
    not a 404.

    The scenario check is vacuous for the create route, which derives
    ``evaluation_id`` from the very scenario it passes on; it stays because the two
    are independent arguments here, so a non-route caller can still desync them.

    The group is row-locked while it is resolved (`acquire_group_for_conversation`)
    so a concurrent delete can't strand this conversation under a dead group, and
    the group's `MAX_CONVERSATION_GROUP_SIZE` cap is enforced (a full group is a 409).

    ``parameters`` is the already-`dump_inference_params`-serialised override dict
    (the route owns that conversion, per the inference-params write contract).

    ``content_protected`` is stamped from the evaluation's effective licence here and never
    re-derived, so a licence changed later does not reach conversations that already exist.

    Raises:
        NotFoundError: If the evaluation is not visible to the caller, or the
            assignment / scenario / group does not belong to the evaluation.
        ConflictError: If the target group is already at capacity, or the scenario
            is not the group's scenario.
    """
    await get_evaluation(session, evaluation_id, caller_id=user_id, can_manage=can_manage, with_models=False)
    await get_assignment(session, evaluation_id, evaluation_ai_model_id)
    await get_scenario(session, evaluation_id, scenario_id)
    group = await acquire_group_for_conversation(
        session, evaluation_id, conversation_group_id, caller_id=user_id, can_manage=can_manage
    )
    if scenario_id != group.scenario_id:
        raise ConflictError(
            f"Scenario {scenario_id} is not the scenario of conversation group {conversation_group_id}; "
            "a group's conversations all share its scenario."
        )
    await assert_tags_allowed(session, evaluation_id, tags or {})
    licence = await resolve_effective_license(session, evaluation_id)
    conversation = Conversation(
        user_id=user_id,
        evaluation_id=evaluation_id,
        evaluation_ai_model_id=evaluation_ai_model_id,
        scenario_id=scenario_id,
        conversation_group_id=conversation_group_id,
        title=title,
        parameters=parameters,
        tags=tags or {},
        content_protected=licence.protects_conversation_data,
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation, attribute_names=["created_at", "updated_at"])
    return conversation


async def update_conversation(
    session: AsyncSession,
    conversation: Conversation,
    *,
    by_id: UUID,
    parameters: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
    new_conversation_group_id: UUID | None = None,
    title: str | None = None,
    title_provided: bool = False,
) -> Conversation:
    """Apply a `parameters` replace, a group **move**, and/or a `title` change, then prune.

    ``parameters`` (the already-serialised override dict) and ``tags`` each replace the
    whole layer rather than merging (sending `{}` clears them). ``new_conversation_group_id``
    moves the conversation to a different group — the **route** must have validated
    it belongs to the same evaluation and the caller and shares the conversation's
    scenario; if the move empties the
    previous group it is removed (every group keeps ≥1 live conversation). Both args
    default to ``None`` meaning "leave unchanged".

    ``title`` is tri-state, so unlike the other two args it can't use ``None`` as its own
    "leave unchanged" sentinel (``None`` is also its legitimate "clear" value) — the
    **route** sets ``title_provided`` from `ConversationUpdate.model_fields_set` to say
    whether the field was present in the payload at all. Passing none of the three is a
    no-op write.
    """
    previous_group_id = conversation.conversation_group_id
    if parameters is not None:
        conversation.parameters = parameters
    if tags is not None:
        await assert_tags_allowed(session, conversation.evaluation_id, tags)
        conversation.tags = tags
    if new_conversation_group_id is not None:
        conversation.conversation_group_id = new_conversation_group_id
    if title_provided:
        conversation.title = title
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation, attribute_names=["updated_at"])
    if new_conversation_group_id is not None and new_conversation_group_id != previous_group_id:
        await soft_delete_empty_groups(session, [previous_group_id], by_id=by_id)
    return conversation


async def soft_delete_conversation(session: AsyncSession, conversation: Conversation, *, by_id: UUID) -> Conversation:
    """Soft-delete ``conversation`` by stamping `deleted_at`, then prune its group.

    A group keeps at least one live conversation; if this was the last one, the
    group is removed too (`soft_delete_empty_groups`).
    """
    conversation_group_id = conversation.conversation_group_id
    conversation.soft_delete(by_id)
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    await soft_delete_empty_groups(session, [conversation_group_id], by_id=by_id)
    return conversation


async def get_restorable_conversation(
    session: AsyncSession,
    evaluation_id: UUID,
    conversation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Conversation:
    """Fetch the tombstoned conversation this caller may restore.

    The read scope of `get_conversation` over the restorable tombstones: outside
    the window, another owner's, another actor's delete, behind a soft-deleted
    evaluation/group, or tombstoned by the model-unassign cascade rather than by a
    user (`_join_live_assignment`) all read as missing.

    Raises:
        NotFoundError: If no such restorable conversation is visible to the caller.
    """
    statement = _join_live_assignment(
        _scope(
            deleted_select(Conversation, deleted_cutoff, deleted_by=None if can_manage else caller_id).where(
                col(Conversation.evaluation_id) == evaluation_id,
                col(Conversation.id) == conversation_id,
            ),
            caller_id=caller_id,
            can_manage=can_manage,
        )
    )
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = statement.with_for_update(of=Conversation).execution_options(populate_existing=True)
    conversation = (await session.execute(statement)).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError(
            f"No restorable conversation {conversation_id} was deleted within the restore window "
            f"in evaluation {evaluation_id}."
        )
    return conversation


async def restore_conversation(session: AsyncSession, conversation: Conversation) -> Conversation:
    """Clear ``conversation``'s tombstone, reviving its group if the delete pruned it.

    The mirror of `soft_delete_conversation`: that call may have removed the group
    left empty, so the group is revived alongside it (`revive_pruned_group`) —
    otherwise the restored conversation would hang off a dead parent and stay
    invisible.

    Restore is a third path that makes a conversation live, so it re-checks the
    group's structural capacity first: the slot freed by the delete may have been
    taken by a new conversation since.

    Raises:
        ConflictError: If the group is already at `max_conversation_group_size`.
    """
    await assert_group_has_room(session, conversation.conversation_group_id)
    await restore_row(session, conversation, conflict_message="Conversation cannot be restored.")
    await revive_pruned_group(session, conversation.conversation_group_id)
    await session.refresh(conversation, attribute_names=["updated_at", "deleted_at", "deleted_by_id"])
    return conversation


async def soft_delete_conversations_for_assignment(
    session: AsyncSession, evaluation_ai_model_id: UUID, *, by_id: UUID
) -> int:
    """Soft-delete every live conversation against ``evaluation_ai_model_id``, returning the count.

    Cascade hook for model-unassignment (a single assignment removed from an
    evaluation): the chosen model is gone, so the conversation can no longer run —
    mark it deleted rather than leave it pointing at a tombstoned assignment. Any
    group left with no live conversation is removed too, so the invariant holds
    under bulk removal as well.
    """
    affected_groups = (
        (
            await session.execute(
                select(col(Conversation.conversation_group_id))
                .where(col(Conversation.evaluation_ai_model_id) == evaluation_ai_model_id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    statement = (
        Conversation.live_update()
        .where(col(Conversation.evaluation_ai_model_id) == evaluation_ai_model_id)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    result = await session.execute(statement)
    await soft_delete_empty_groups(session, affected_groups, by_id=by_id)
    return result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result


async def soft_delete_conversations_for_model(session: AsyncSession, model_id: UUID, *, by_id: UUID) -> int:
    """Soft-delete live conversations whose assigned model is ``model_id``, returning the count.

    Cascade hook for AI-model removal, layered on `unassign_models_for_model`: that
    bulk-soft-deletes the model's assignments, so the conversations that chose one
    of them must go too. Keys off `EvaluationAiModel.model_id` regardless of the
    assignment's own liveness (the assignment is tombstoned in the same request).
    Groups emptied by the cascade are removed too.
    """
    assignment_ids = select(col(EvaluationAiModel.id)).where(col(EvaluationAiModel.model_id) == model_id)
    affected_groups = (
        (
            await session.execute(
                select(col(Conversation.conversation_group_id))
                .where(col(Conversation.evaluation_ai_model_id).in_(assignment_ids))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    statement = (
        Conversation.live_update()
        .where(col(Conversation.evaluation_ai_model_id).in_(assignment_ids))
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    result = await session.execute(statement)
    await soft_delete_empty_groups(session, affected_groups, by_id=by_id)
    return result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result
