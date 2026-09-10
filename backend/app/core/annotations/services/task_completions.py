"""Task-completion service — pure async functions over an `AsyncSession`.

A `TaskCompletion` is a red-teamer's check-off of one scenario task within one
conversation. It reuses the message-flag scope model wholesale: owner-scoped
(`created_by_id == caller_id`) and gated by the parent group's visibility, with
the `evaluation_groups:manage` break-glass (`can_manage`) lifting both the owner
and visibility predicates on *reads*; parent liveness (conversation, evaluation,
group) is always enforced, so a soft-deleted ancestor hides the completion.

Authoring is **owner-only** (like `create_flag`): you can only check off a task
in a conversation you own — `resolve_conversation_context` requires
`Conversation.user_id == caller_id`, and `can_manage` does not lift it. Toggling
is idempotent: `complete_task` returns the existing live row (or inserts one),
`uncomplete_task` soft-deletes the live row (or no-ops). A live row means the
task is done; the soft-delete trail is the completion history.
"""

from collections import Counter
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.annotations.models import TaskCompletion
from app.core.annotations.services.message_flags import resolve_conversation_context
from app.core.conversations.models import Conversation
from app.core.evaluations.access import join_conversation_scoped
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.models import Task
from app.core.exceptions import NotFoundError


def _scope(
    statement: Select[tuple[TaskCompletion]], *, caller_id: UUID, can_manage: bool
) -> Select[tuple[TaskCompletion]]:
    """Constrain to the caller's own, group-visible completions on a live conversation.

    The shared `join_conversation_scoped` scope, used by both the entity read and the
    aggregate roll-up.
    """
    return join_conversation_scoped(statement, TaskCompletion, caller_id=caller_id, can_manage=can_manage)


async def _get_owned_live_completion(
    session: AsyncSession, conversation_id: UUID, task_id: UUID, caller_id: UUID
) -> TaskCompletion | None:
    """Fetch the caller's live completion for ``(conversation_id, task_id)``, if any.

    Owner-scoped by `created_by_id`; used on the mutation paths where the caller's
    ownership of the conversation is already established by
    `resolve_conversation_context`.
    """
    return (
        await session.execute(
            TaskCompletion.live_select().where(
                col(TaskCompletion.conversation_id) == conversation_id,
                col(TaskCompletion.task_id) == task_id,
                col(TaskCompletion.created_by_id) == caller_id,
            )
        )
    ).scalar_one_or_none()


async def _assert_task_in_conversation_scenario(
    session: AsyncSession, conversation: Conversation, task_id: UUID
) -> None:
    """Verify ``task_id`` is a live task of ``conversation``'s scenario (mirrors `create_flag`).

    Raises:
        NotFoundError: If the task is not a live task of that scenario.
    """
    task = (
        await session.execute(
            Task.live_select().where(
                col(Task.id) == task_id,
                col(Task.scenario_id) == conversation.scenario_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(f"Task {task_id} not found in scenario {conversation.scenario_id}.")


async def complete_task(
    session: AsyncSession, *, conversation_id: UUID, task_id: UUID, caller_id: UUID
) -> TaskCompletion:
    """Check off ``task_id`` in ``conversation_id`` for ``caller_id`` (idempotent).

    Denormalises the resolved conversation's ancestry onto the row. A concurrent
    insert racing the unique `(conversation_id, task_id)` index is caught and
    resolved to the winning row, so the call is idempotent under contention too.

    Raises:
        NotFoundError: If the conversation isn't reachable by the caller (a clean
            404, not an FK violation), or ``task_id`` isn't a live task of its scenario.
    """
    conversation, evaluation_group_id = await resolve_conversation_context(
        session, conversation_id, caller_id=caller_id
    )
    await _assert_task_in_conversation_scenario(session, conversation, task_id)

    existing = await _get_owned_live_completion(session, conversation_id, task_id, caller_id)
    if existing is not None:
        return existing

    completion = TaskCompletion(
        created_by_id=caller_id,
        conversation_id=conversation.id,
        task_id=task_id,
        conversation_group_id=conversation.conversation_group_id,
        evaluation_id=conversation.evaluation_id,
        evaluation_group_id=evaluation_group_id,
        scenario_id=conversation.scenario_id,
    )
    try:
        async with session.begin_nested():
            session.add(completion)
            await session.flush()
    except IntegrityError:
        # Lost the race on the partial-unique index — the concurrent insert's row
        # is authoritative. Re-read and return it (idempotent success). The re-read is
        # owner-scoped while the index is global on (conversation_id, task_id); that's
        # safe because completion is owner-only and a conversation has a single owner, so
        # the racing insert is necessarily this same caller. If ownership ever becomes
        # transferable, narrow the unique index to include created_by_id.
        won = await _get_owned_live_completion(session, conversation_id, task_id, caller_id)
        if won is None:
            raise
        return won
    await session.refresh(completion, attribute_names=["created_at", "updated_at"])
    return completion


async def uncomplete_task(session: AsyncSession, *, conversation_id: UUID, task_id: UUID, caller_id: UUID) -> None:
    """Uncheck ``task_id`` in ``conversation_id`` for ``caller_id`` (idempotent).

    Owner-only, like `complete_task`: resolves the conversation under the caller's
    ownership (unreachable → 404), then soft-deletes the live completion for the
    pair. No live completion is a no-op — unchecking an unchecked task succeeds.

    Raises:
        NotFoundError: If the conversation isn't reachable by the caller.
    """
    await resolve_conversation_context(session, conversation_id, caller_id=caller_id)
    completion = await _get_owned_live_completion(session, conversation_id, task_id, caller_id)
    if completion is None:
        return
    completion.soft_delete(caller_id)
    session.add(completion)
    await session.flush()


async def list_conversation_completions(
    session: AsyncSession, conversation_id: UUID, *, caller_id: UUID, can_manage: bool = False
) -> list[TaskCompletion]:
    """Return the caller's live completions for ``conversation_id`` (unpaginated).

    Bounded by the scenario's task count, so it returns the full set — the client
    renders one checkbox per task and marks the ones present here. Scoped like the
    flag reads; `can_manage` lifts the owner/visibility predicates.
    """
    statement = _scope(
        TaskCompletion.live_select().where(col(TaskCompletion.conversation_id) == conversation_id),
        caller_id=caller_id,
        can_manage=can_manage,
    ).order_by(col(TaskCompletion.created_at), col(TaskCompletion.id))
    return list((await session.execute(statement)).scalars().all())


async def group_completion_rollup(
    session: AsyncSession, conversation_group_id: UUID, *, caller_id: UUID, can_manage: bool = False
) -> tuple[int, dict[UUID, int]]:
    """Roll completions up across one conversation group: `(total_conversations, {task_id: count})`.

    ``total_conversations`` is the group's live conversation count (the "N"); the
    map gives, per task, how many distinct conversations of the group have it
    checked off (the "K"). Both are scoped through the shared visibility spine and
    the caller's ownership (lifted by ``can_manage``), so a group the caller can't
    reach rolls up as empty rather than leaking. Tasks with no completion are
    absent from the map — the client defaults them to 0.
    """
    conversations_stmt = join_visible_evaluation_group(
        select(func.count()).select_from(Conversation),
        col(Conversation.evaluation_id),
        caller_id=caller_id,
        can_manage=can_manage,
    ).where(
        col(Conversation.conversation_group_id) == conversation_group_id,
        col(Conversation.deleted_at).is_(None),
    )
    if not can_manage:
        conversations_stmt = conversations_stmt.where(col(Conversation.user_id) == caller_id)
    total_conversations = (await session.execute(conversations_stmt)).scalar_one()

    # Scope by the conversation's CURRENT group (the live `Conversation` joined in
    # `_scope`), NOT the completion's denormalised `conversation_group_id`: that
    # snapshot goes stale when a conversation is moved between groups (same
    # evaluation), which would make the moved conversation's completions count under
    # its old group and vanish from the new one. Aggregate in Python from the scoped
    # live rows — a group holds a handful of conversations times a scenario's handful
    # of tasks, so the set stays small.
    completions_stmt = _scope(
        TaskCompletion.live_select(),
        caller_id=caller_id,
        can_manage=can_manage,
    ).where(col(Conversation.conversation_group_id) == conversation_group_id)
    # One live row per (conversation_id, task_id) is guaranteed by the partial-unique
    # index, so counting rows per task_id already counts distinct conversations.
    counts = Counter(c.task_id for c in (await session.execute(completions_stmt)).scalars().all())
    return total_conversations, dict(counts)
