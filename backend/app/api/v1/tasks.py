"""Task endpoints — nested under `/scenarios/{scenario_id}/tasks`.

A task is the simple (name + description) sub-level of a `Scenario`. Editing a
task *is* editing its evaluation: reads gate on `evaluations:read` and mutations
on `evaluations:update`, with authorization inherited from the scenario's parent
group — reads resolve only tasks of a `public`-or-member (live) group; mutations
require an in-group role granting `evaluations:update` on the parent group, or
the `evaluation_groups:manage` break-glass.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status

from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.evaluations.models import Task
from app.core.evaluations.schemas import TaskCreate
from app.core.evaluations.schemas import TaskResponse
from app.core.evaluations.schemas import TaskUpdate
from app.core.evaluations.schemas import TaskUpdateChanges
from app.core.evaluations.services.scenarios import authorize_scenario_mutation
from app.core.evaluations.services.scenarios import get_scenario_by_id
from app.core.evaluations.services.tasks import create_task
from app.core.evaluations.services.tasks import get_restorable_task
from app.core.evaluations.services.tasks import get_task
from app.core.evaluations.services.tasks import list_tasks
from app.core.evaluations.services.tasks import restore_task
from app.core.evaluations.services.tasks import soft_delete_task
from app.core.evaluations.services.tasks import update_task
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilterNewestFirst
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/scenarios/{scenario_id}/tasks", tags=["tasks"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission on the parent group.")
_NOT_FOUND = problem_response("Scenario or task does not exist, or its parent group is not visible.")

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))]


def _task_snapshot(task: Task) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after."""
    return {"name": task.name, "description": task.description}


_WRITE_RESPONSES = COMMON_ERROR_RESPONSES | {
    status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    status.HTTP_404_NOT_FOUND: _NOT_FOUND,
}
# Reads carry no group dimension: a parent the caller cannot see is a 404 from `get_task`, and
# `require_permission` checks a global claim. The list route is the exception — its `deleted=true`
# branch is authorized as a write — so it overrides with the wider wording below.
_READ_FORBIDDEN = problem_response("Caller lacks the required permission.")
_LIST_FORBIDDEN = problem_response(
    "Caller lacks the required permission, or — for `deleted=true` — write access to the parent group."
)
_READ_RESPONSES = COMMON_ERROR_RESPONSES | {
    status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: _READ_FORBIDDEN,
    status.HTTP_404_NOT_FOUND: _NOT_FOUND,
}


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create task",
    description="Add a task to a scenario.",
    responses=_WRITE_RESPONSES,
)
@transactional
async def create_task_endpoint(
    scenario_id: UUID,
    payload: TaskCreate,
    caller: _UpdateCaller,
    db: DbSession,
    response: Response,
) -> TaskResponse:
    """Create a task within a scenario.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller's in-group role doesn't grant `evaluations:update` on the parent group.
    * **404 Not Found** — the scenario does not exist or its parent group isn't visible.
    """
    await authorize_scenario_mutation(db, scenario_id, caller_id=caller.id, can_manage=_can_manage(caller))
    task = await create_task(db, scenario_id=scenario_id, name=payload.name, description=payload.description)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.TASK_CREATE,
        object_type="task",
        object_id=task.id,
        after=_task_snapshot(task),
        context={"scenario_id": str(scenario_id)},
    )
    response.headers["Location"] = f"/api/v1/scenarios/{scenario_id}/tasks/{task.id}"
    return TaskResponse.from_model(task)


@router.get(
    "",
    response_model=Page[TaskResponse],
    status_code=status.HTTP_200_OK,
    summary="List tasks in a scenario",
    description=(
        "Return the scenario's tasks, ordered by creation. `deleted=true` serves the tasks deleted "
        "inside the restore window instead, most-recently-deleted first — the set "
        "`POST …/{task_id}/restore` can act on, so it requires exactly what the delete and the restore "
        "do: the `evaluations:update` permission **and** write access to the parent group."
    ),
    responses=_READ_RESPONSES | {status.HTTP_403_FORBIDDEN: _LIST_FORBIDDEN},
)
async def list_tasks_endpoint(
    scenario_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    deleted: DeletedFilterNewestFirst = False,
) -> Page[TaskResponse]:
    """List the tasks of one scenario.

    `deleted=true` is the restore surface, so it is authorized as a write rather than a
    read — **both** halves the delete and the restore require: the global
    `evaluations:update` permission *and* write access to the parent group. Without the
    second an in-group reader holding `evaluations:update` globally would list tombstones
    and then get 403 from every Restore. It then serves only tombstones the caller can act
    on: their own deletes, or every one with `evaluation_groups:manage`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller does not hold `evaluations:read`; or asked for `deleted=true`
      without `evaluations:update`, or without write access to the parent group.
    * **404 Not Found** — the scenario does not exist or its parent group isn't visible.
    """
    can_manage = _can_manage(caller)
    assert_may_list_deleted(deleted, caller, Permission.EVALUATIONS_UPDATE, entity="tasks")
    if deleted:
        # The write gate resolves the scenario too, so the deleted branch pays the extra
        # round-trip the live path defers to its empty case below.
        await authorize_scenario_mutation(db, scenario_id, caller_id=caller.id, can_manage=can_manage)
    tasks, total = await list_tasks(
        db,
        scenario_id=scenario_id,
        caller_id=caller.id,
        can_manage=can_manage,
        deleted=deleted,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    if total == 0:
        # `list_tasks` already scopes to visible rows, so 0 results is ambiguous: a
        # hidden/absent scenario (→ 404, parity with the scenario list) or a visible
        # one with no tasks (→ 200 empty page). Resolve the scenario only in this
        # rare case — the common (non-empty) list drops from 3 round-trips to 2.
        await get_scenario_by_id(db, scenario_id, caller_id=caller.id, can_manage=can_manage)
    return Page[TaskResponse](
        items=[TaskResponse.from_model(task) for task in tasks],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Get task",
    description="Fetch one task within a scenario by id.",
    responses=_READ_RESPONSES,
)
async def get_task_endpoint(
    scenario_id: UUID,
    task_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> TaskResponse:
    """Fetch one task.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller does not hold `evaluations:read`.
    * **404 Not Found** — no such task in the scenario, or its parent group isn't visible.
    """
    task = await get_task(db, scenario_id, task_id, caller_id=caller.id, can_manage=_can_manage(caller))
    return TaskResponse.from_model(task)


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Update task",
    description="Partially update a task's `name` / `description`. Omitted fields stay.",
    responses=_WRITE_RESPONSES,
)
@transactional
async def update_task_endpoint(
    scenario_id: UUID,
    task_id: UUID,
    payload: TaskUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> TaskResponse:
    """Partially update a task.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller's in-group role doesn't grant `evaluations:update` on the parent group.
    * **404 Not Found** — no such task in the scenario, or its parent group isn't visible.
    """
    await authorize_scenario_mutation(db, scenario_id, caller_id=caller.id, can_manage=_can_manage(caller))
    task = await get_task(db, scenario_id, task_id, for_update=True)
    before = _task_snapshot(task)
    changes = TaskUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_task(db, task, changes)
    diff_before, diff_after = changed_fields(before, _task_snapshot(updated))
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.TASK_UPDATE,
        object_type="task",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return TaskResponse.from_model(updated)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete task",
    description="Soft-delete a task (sets `deleted_at`); subsequent reads exclude it.",
    responses=_WRITE_RESPONSES,
)
@transactional
async def delete_task_endpoint(
    scenario_id: UUID,
    task_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a task.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller's in-group role doesn't grant `evaluations:update` on the parent group.
    * **404 Not Found** — no such task in the scenario, or its parent group isn't visible.
    """
    await authorize_scenario_mutation(db, scenario_id, caller_id=caller.id, can_manage=_can_manage(caller))
    task = await get_task(db, scenario_id, task_id, for_update=True)
    before = _task_snapshot(task)
    await soft_delete_task(db, task, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.TASK_DELETE,
        object_type="task",
        object_id=task_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{task_id}/restore",
    response_model=TaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore task",
    description=(
        "Clear a soft-deleted task's `deleted_at`. Restorable for a fixed window after the delete "
        "(set per deployment), and authorized like the delete it undoes — by the user who deleted it, "
        "or by an `evaluation_groups:manage` break-glass holder. The "
        "per-participant completions recorded against it were never tombstoned, so they come back "
        "with it — which is why restoring beats re-creating. Not restorable once the parent scenario "
        "is soft-deleted: restore the scenario instead, which brings its tasks back."
    ),
    responses=_WRITE_RESPONSES,
)
@transactional
async def restore_task_endpoint(
    scenario_id: UUID,
    task_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
    settings: SettingsDep,
) -> TaskResponse:
    """Restore a soft-deleted task.

    Narrowed to the caller's own deletes unless they hold `evaluation_groups:manage`,
    exactly as the deleted listing is.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller's in-group role doesn't grant `evaluations:update` on the parent group.
    * **404 Not Found** — no restorable task: never deleted, outside the restore window,
      another user's delete, or its parent scenario is soft-deleted.
    """
    can_manage = _can_manage(caller)
    await authorize_scenario_mutation(db, scenario_id, caller_id=caller.id, can_manage=can_manage)
    task = await get_restorable_task(
        db, scenario_id, task_id, caller_id=caller.id, can_manage=can_manage, deleted_cutoff=restore_cutoff(settings)
    )
    restored = await restore_task(db, task)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.TASK_RESTORE,
        object_type="task",
        object_id=task_id,
        after=_task_snapshot(restored),
    )
    return TaskResponse.from_model(restored)
