"""Scenario endpoints — nested `/evaluations/{evaluation_id}/scenarios` plus a standalone `/scenarios`.

Scenario editing *is* evaluation editing: reads gate on `evaluations:read` and
mutations on `evaluations:update`, and — like the model-assignment sub-resource —
authorization is inherited one level down from the parent group. Reads resolve
only scenarios of a `public`-or-member (live) group; mutations require an
in-group role granting `evaluations:update`, or `evaluation_groups:manage`.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
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
from app.core.evaluations.dependencies import ScenarioFiltersDep
from app.core.evaluations.filters import ScenarioFilters
from app.core.evaluations.filters import ScenarioOrderBy
from app.core.evaluations.models import Scenario
from app.core.evaluations.schemas import ScenarioCreate
from app.core.evaluations.schemas import ScenarioDetailResponse
from app.core.evaluations.schemas import ScenarioReorderRequest
from app.core.evaluations.schemas import ScenarioResponse
from app.core.evaluations.schemas import ScenarioUpdate
from app.core.evaluations.schemas import ScenarioUpdateChanges
from app.core.evaluations.services.evaluations import authorize_evaluation_mutation
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.scenarios import create_scenario
from app.core.evaluations.services.scenarios import get_restorable_scenario
from app.core.evaluations.services.scenarios import get_scenario
from app.core.evaluations.services.scenarios import list_scenarios
from app.core.evaluations.services.scenarios import reorder_scenarios
from app.core.evaluations.services.scenarios import restore_scenario
from app.core.evaluations.services.scenarios import soft_delete_scenario
from app.core.evaluations.services.scenarios import update_scenario
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilter
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(tags=["scenarios"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission, or does not own the parent group.")
_NOT_FOUND = problem_response("Evaluation or scenario does not exist, or its parent group is not visible.")
_NOT_RESTORABLE = problem_response(
    "No restorable scenario with this id in this evaluation: never deleted, deleted longer than the "
    "restore window ago, deleted by another user, or its parent evaluation or group is soft-deleted."
)
_REORDER_CONFLICT = problem_response("`scenario_ids` does not match the evaluation's scenarios exactly.")

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))]


def _scenario_snapshot(scenario: Scenario) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after."""
    return {
        "name": scenario.name,
        "description": scenario.description,
        "required_reviews": scenario.required_reviews,
    }


@router.post(
    "/evaluations/{evaluation_id}/scenarios",
    response_model=ScenarioResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create scenario",
    description="Add a scenario to an evaluation. It is appended last (highest `position`).",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def create_scenario_endpoint(
    evaluation_id: UUID,
    payload: ScenarioCreate,
    caller: _UpdateCaller,
    db: DbSession,
    response: Response,
) -> ScenarioResponse:
    """Create a scenario within an evaluation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or doesn't own the parent group.
    * **404 Not Found** — the evaluation does not exist or its parent group isn't visible.
    """
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=_can_manage(caller), permission=Permission.EVALUATIONS_UPDATE
    )
    scenario = await create_scenario(
        db,
        evaluation_id=evaluation_id,
        name=payload.name,
        description=payload.description,
        required_reviews=payload.required_reviews,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.SCENARIO_CREATE,
        object_type="scenario",
        object_id=scenario.id,
        after=_scenario_snapshot(scenario),
        context={"evaluation_id": str(evaluation_id)},
    )
    response.headers["Location"] = f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario.id}"
    return ScenarioResponse.from_model(scenario)


@router.get(
    "/evaluations/{evaluation_id}/scenarios",
    response_model=Page[ScenarioResponse],
    status_code=status.HTTP_200_OK,
    summary="List scenarios in an evaluation",
    description=(
        "Return the evaluation's scenarios, ordered by `position` by default. `deleted=true` serves the "
        "scenarios deleted inside the restore window instead — the set `POST …/{scenario_id}/restore` can "
        "act on, so it requires exactly what the delete and the restore do: the `evaluations:update` "
        "permission **and** write access to the parent group."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def list_evaluation_scenarios_endpoint(
    evaluation_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on name or description.", max_length=255),
    ] = None,
    deleted: DeletedFilter = False,
    order_by: Annotated[
        ScenarioOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "position",
) -> Page[ScenarioResponse]:
    """List the scenarios of one evaluation.

    `deleted=true` is the restore surface, so it is authorized as a write rather than a
    read — **both** halves the delete and the restore require: the global
    `evaluations:update` permission *and* write access to the parent group. Without the
    second an in-group reader holding `evaluations:update` globally would list tombstones
    and then get 403 from every Restore. It then serves only tombstones the caller can act
    on: their own deletes, or every one with `evaluation_groups:manage`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission, or asked for
      `deleted=true` without `evaluations:update` or without write access to the parent group.
    * **404 Not Found** — the evaluation does not exist or its parent group isn't visible.
    """
    can_manage = _can_manage(caller)
    assert_may_list_deleted(deleted, caller, Permission.EVALUATIONS_UPDATE, entity="scenarios")
    # Resolve the evaluation under the visibility rule first, so an unknown/hidden
    # parent is a 404 (parity with the model-assignment list), not a 200 empty page.
    await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage, with_models=False)
    if deleted:
        await authorize_evaluation_mutation(
            db,
            evaluation_id,
            caller_id=caller.id,
            can_manage=can_manage,
            permission=Permission.EVALUATIONS_UPDATE,
        )
    filters = ScenarioFilters(evaluation_id=evaluation_id, search=search, deleted=deleted)
    scenarios, total = await list_scenarios(
        db,
        caller_id=caller.id,
        can_manage=can_manage,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[ScenarioResponse](
        items=[ScenarioResponse.from_model(scenario) for scenario in scenarios],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.patch(
    "/evaluations/{evaluation_id}/scenarios/order",
    response_model=list[ScenarioResponse],
    status_code=status.HTTP_200_OK,
    summary="Reorder scenarios",
    description=(
        "Rewrite scenario order: `scenario_ids` is the full ordered set of the evaluation's scenarios; "
        "each scenario's new `position` is its index in the list. Returns the scenarios in the new order."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _REORDER_CONFLICT,
    },
)
@transactional
async def reorder_scenarios_endpoint(
    evaluation_id: UUID,
    payload: ScenarioReorderRequest,
    caller: _UpdateCaller,
    db: DbSession,
) -> list[ScenarioResponse]:
    """Reorder all scenarios of an evaluation in one transaction.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or doesn't own the parent group.
    * **404 Not Found** — the evaluation isn't visible, or an id is not a scenario of it.
    * **409 Conflict** — `scenario_ids` omits some of the evaluation's scenarios.
    """
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=_can_manage(caller), permission=Permission.EVALUATIONS_UPDATE
    )
    scenarios = await reorder_scenarios(db, evaluation_id, payload.scenario_ids)
    # Record the authoritative resulting order the service re-read from the DB (ordered by the
    # new `position`), not the requested payload — so the audit reflects what actually landed.
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.SCENARIO_REORDER,
        object_type="evaluation",
        object_id=evaluation_id,
        after={"order": [str(scenario.id) for scenario in scenarios]},
    )
    return [ScenarioResponse.from_model(scenario) for scenario in scenarios]


@router.get(
    "/evaluations/{evaluation_id}/scenarios/{scenario_id}",
    response_model=ScenarioDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get scenario",
    description="Fetch one scenario within an evaluation by id, with its tasks embedded.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_scenario_endpoint(
    evaluation_id: UUID,
    scenario_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> ScenarioDetailResponse:
    """Fetch one scenario, with its tasks embedded.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission.
    * **404 Not Found** — no such scenario in this evaluation, or its parent group isn't visible.
    """
    scenario = await get_scenario(
        db, evaluation_id, scenario_id, caller_id=caller.id, can_manage=_can_manage(caller), with_tasks=True
    )
    return ScenarioDetailResponse.from_model_with_tasks(scenario)


@router.patch(
    "/evaluations/{evaluation_id}/scenarios/{scenario_id}",
    response_model=ScenarioResponse,
    status_code=status.HTTP_200_OK,
    summary="Update scenario",
    description=(
        "Partially update a scenario's `name` / `description`. Omitted fields stay; `position` is not editable here."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_scenario_endpoint(
    evaluation_id: UUID,
    scenario_id: UUID,
    payload: ScenarioUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> ScenarioResponse:
    """Partially update a scenario.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or doesn't own the parent group.
    * **404 Not Found** — no such scenario in this evaluation, or its parent group isn't visible.
    """
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=_can_manage(caller), permission=Permission.EVALUATIONS_UPDATE
    )
    scenario = await get_scenario(db, evaluation_id, scenario_id, for_update=True)
    before = _scenario_snapshot(scenario)
    changes = ScenarioUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_scenario(db, scenario, changes)
    diff_before, diff_after = changed_fields(before, _scenario_snapshot(updated))
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.SCENARIO_UPDATE,
        object_type="scenario",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return ScenarioResponse.from_model(updated)


@router.delete(
    "/evaluations/{evaluation_id}/scenarios/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete scenario",
    description="Soft-delete a scenario (sets `deleted_at`); subsequent reads exclude it.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_scenario_endpoint(
    evaluation_id: UUID,
    scenario_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a scenario.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or doesn't own the parent group.
    * **404 Not Found** — no such scenario in this evaluation, or its parent group isn't visible.
    """
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=_can_manage(caller), permission=Permission.EVALUATIONS_UPDATE
    )
    scenario = await get_scenario(db, evaluation_id, scenario_id, for_update=True)
    before = _scenario_snapshot(scenario)
    await soft_delete_scenario(db, scenario, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.SCENARIO_DELETE,
        object_type="scenario",
        object_id=scenario_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/evaluations/{evaluation_id}/scenarios/{scenario_id}/restore",
    response_model=ScenarioResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore scenario",
    description=(
        "Clear a soft-deleted scenario's `deleted_at`. Restorable for a fixed window after the delete "
        "(set per deployment), and authorized like the delete it undoes — by the user who deleted it, "
        "or by an `evaluation_groups:manage` break-glass holder. Its tasks come "
        "back with it — the delete never tombstoned them. The scenario is appended **last** in the "
        "evaluation rather than returned to its old `position`, which a reorder since may have taken. "
        "Not restorable once the parent evaluation or group is soft-deleted."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
    },
)
@transactional
async def restore_scenario_endpoint(
    evaluation_id: UUID,
    scenario_id: UUID,
    caller: _UpdateCaller,
    db: DbSession,
    settings: SettingsDep,
) -> ScenarioResponse:
    """Restore a soft-deleted scenario, appended last in its evaluation.

    Narrowed to the caller's own deletes unless they hold `evaluation_groups:manage`,
    exactly as the deleted listing is.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or doesn't own the parent group.
    * **404 Not Found** — no restorable scenario: never deleted, outside the restore
      window, another user's delete, or its parent evaluation/group is soft-deleted.
    """
    can_manage = _can_manage(caller)
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    scenario = await get_restorable_scenario(
        db,
        evaluation_id,
        scenario_id,
        caller_id=caller.id,
        can_manage=can_manage,
        deleted_cutoff=restore_cutoff(settings),
    )
    restored = await restore_scenario(db, scenario)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.SCENARIO_RESTORE,
        object_type="scenario",
        object_id=scenario_id,
        after=_scenario_snapshot(restored),
    )
    return ScenarioResponse.from_model(restored)


@router.get(
    "/scenarios",
    response_model=Page[ScenarioResponse],
    status_code=status.HTTP_200_OK,
    summary="List scenarios across evaluations",
    description=(
        "Flat, paginated list of scenarios; filter with `evaluation_id` and/or `search`. "
        "Ordered by recency (`-created_at`) by default."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_scenarios_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: ScenarioFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: Annotated[
        ScenarioOrderBy,
        Query(
            description=(
                "Column to order by; prefix with `-` for descending. Defaults to `-created_at`: "
                "`position` is per-evaluation, so ordering a cross-evaluation list by it interleaves evaluations."
            ),
        ),
    ] = "-created_at",
) -> Page[ScenarioResponse]:
    """List scenarios across all evaluations visible to the caller.

    Unlike the nested list, an `evaluation_id` filter pointing at an evaluation
    the caller can't see (missing or a non-visible group) yields an empty page,
    not a 404 — this is a flat filtered view, not a sub-resource of one evaluation,
    and an empty result leaks nothing about the target's existence.

    `deleted=true` behaves as on the nested list: `evaluations:update` required, and
    the set is scoped to the caller's own deletes unless they hold
    `evaluation_groups:manage`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission, or asked for
      `deleted=true` without `evaluations:update`.
    """
    assert_may_list_deleted(filters.deleted, caller, Permission.EVALUATIONS_UPDATE, entity="scenarios")
    scenarios, total = await list_scenarios(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[ScenarioResponse](
        items=[ScenarioResponse.from_model(scenario) for scenario in scenarios],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
