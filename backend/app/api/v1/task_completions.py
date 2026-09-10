"""Task-completion endpoints — a red-teamer checking off scenario tasks.

Completions are owner-scoped progress markers: a red-teamer checks off a
scenario's tasks within a conversation they own. Mutations gate on
`conversations:update` (held by `red_teamer` + `admin`) and are owner-only; reads
gate on `conversations:read`, with the `evaluation_groups:manage` break-glass
lifting the owner/visibility scope. A completion is always per conversation and
per task, written through the conversation routes; the group route is a read-only
"K/N conversations" roll-up over those same rows.

The routes hang off the existing parents (`/conversations/{id}` and
`/conversation-groups/{id}`) rather than a flat resource, so `task_id` is a live
task of the conversation's scenario and the group id is an existing group.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status

from app.core.annotations.schemas import GroupTaskCompletionRollup
from app.core.annotations.schemas import TaskCompletionCount
from app.core.annotations.schemas import TaskCompletionResponse
from app.core.annotations.services.task_completions import complete_task
from app.core.annotations.services.task_completions import group_completion_rollup
from app.core.annotations.services.task_completions import list_conversation_completions
from app.core.annotations.services.task_completions import uncomplete_task
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.dependencies import group_of_path_conversation
from app.core.conversations.dependencies import group_of_path_conversation_group
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.evaluations.dependencies import require_object_or_global
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(tags=["task-completions"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response(
    "The conversation does not exist (or is not the caller's / not visible), "
    "or the task is not part of the conversation's scenario."
)
# DELETE never validates task-in-scenario (an unknown task_id is a 204 no-op), so its
# only 404 is the conversation being unreachable — a narrower body than the PUT's.
_NOT_FOUND_CONVERSATION = problem_response("The conversation does not exist (or is not the caller's / not visible).")

# The JWT carries global roles only, so these gates also accept the permission from a role
# held on the conversation's parent group — otherwise the checklist 403s for a group-scoped
# red teamer on every load, on a page they are otherwise entitled to use.
_ReadCaller = Annotated[
    SessionUser, Depends(require_object_or_global(Permission.CONVERSATIONS_READ, group_of_path_conversation))
]
_UpdateCaller = Annotated[
    SessionUser, Depends(require_object_or_global(Permission.CONVERSATIONS_UPDATE, group_of_path_conversation))
]
_GroupReadCaller = Annotated[
    SessionUser, Depends(require_object_or_global(Permission.CONVERSATIONS_READ, group_of_path_conversation_group))
]


@router.put(
    "/conversations/{conversation_id}/completed-tasks/{task_id}",
    response_model=TaskCompletionResponse,
    status_code=status.HTTP_200_OK,
    summary="Check off a task",
    description=(
        "Mark a scenario task as completed in a conversation the caller owns. Idempotent — re-marking an "
        "already-checked task returns the existing completion. `task_id` must be a task of the conversation's scenario."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def complete_task_endpoint(
    conversation_id: UUID,
    task_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
) -> TaskCompletionResponse:
    """Check off a scenario task in a conversation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:update`.
    * **404 Not Found** — the conversation isn't the caller's / not visible, or the task isn't part of its scenario.
    """
    completion = await complete_task(db, conversation_id=conversation_id, task_id=task_id, caller_id=caller.id)
    return TaskCompletionResponse.from_model(completion)


@router.delete(
    "/conversations/{conversation_id}/completed-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Uncheck a task",
    description=(
        "Unmark a scenario task in a conversation the caller owns (soft-deletes the completion). Idempotent — "
        "unchecking a task that isn't checked succeeds with no change."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND_CONVERSATION,
    },
)
@transactional
async def uncomplete_task_endpoint(
    conversation_id: UUID,
    task_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
) -> Response:
    """Uncheck a scenario task in a conversation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:update`.
    * **404 Not Found** — the conversation isn't the caller's / not visible.
    """
    await uncomplete_task(db, conversation_id=conversation_id, task_id=task_id, caller_id=caller.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversations/{conversation_id}/completed-tasks",
    response_model=list[TaskCompletionResponse],
    status_code=status.HTTP_200_OK,
    summary="List a conversation's completed tasks",
    description=(
        "The caller's completed tasks in one conversation — the full (unpaginated) set, bounded by the "
        "scenario's task count, so the client can render one checkbox per task and mark the ones present here."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_conversation_completed_tasks_endpoint(
    conversation_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> list[TaskCompletionResponse]:
    """List the caller's completed tasks in a conversation.

    A conversation the caller can't see yields an empty list, not a 404 — the
    listing never confirms a foreign conversation's existence.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    """
    completions = await list_conversation_completions(
        db, conversation_id, caller_id=caller.id, can_manage=_can_manage(caller)
    )
    return [TaskCompletionResponse.from_model(completion) for completion in completions]


@router.get(
    "/conversation-groups/{conversation_group_id}/completed-tasks",
    response_model=GroupTaskCompletionRollup,
    status_code=status.HTTP_200_OK,
    summary="Roll up a group's completed tasks",
    description=(
        "Read-only per-task roll-up across a conversation group: for each task, how many of the group's "
        "conversations have it checked off (K), plus the group's total conversation count (N). Tasks with no "
        "completion are omitted — default them to 0 client-side."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def group_completed_tasks_rollup_endpoint(
    conversation_group_id: UUID,
    caller: _GroupReadCaller,
    db: DbSession,
) -> GroupTaskCompletionRollup:
    """Roll up completed tasks across a conversation group.

    A group the caller can't reach rolls up as empty (`total_conversations: 0`),
    not a 404.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    """
    total, counts = await group_completion_rollup(
        db, conversation_group_id, caller_id=caller.id, can_manage=_can_manage(caller)
    )
    return GroupTaskCompletionRollup(
        total_conversations=total,
        completed_counts=[
            TaskCompletionCount(task_id=task_id, completed_count=count) for task_id, count in counts.items()
        ],
    )
