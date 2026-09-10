"""Evaluation endpoints — mounted under `/api/v1/evaluations`.

Carries evaluation CRUD plus the model-assignment sub-resource
(`/{evaluation_id}/models`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.ai_gateway import dump_inference_params
from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.schemas import MAX_TAGS
from app.core.conversations.services.conversations import soft_delete_conversations_for_assignment
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.dependencies import EvaluationAiModelFiltersDep
from app.core.evaluations.dependencies import EvaluationFiltersDep
from app.core.evaluations.filters import EvaluationAiModelOrderBy
from app.core.evaluations.filters import EvaluationOrderBy
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.schemas import EvaluationAiModelAssign
from app.core.evaluations.schemas import EvaluationAiModelResponse
from app.core.evaluations.schemas import EvaluationAiModelUpdate
from app.core.evaluations.schemas import EvaluationAiModelUpdateChanges
from app.core.evaluations.schemas import EvaluationAiModelView
from app.core.evaluations.schemas import EvaluationCreate
from app.core.evaluations.schemas import EvaluationRejectRequest
from app.core.evaluations.schemas import EvaluationResponse
from app.core.evaluations.schemas import EvaluationTagKeyCreate
from app.core.evaluations.schemas import EvaluationTagKeyResponse
from app.core.evaluations.schemas import EvaluationUpdate
from app.core.evaluations.services.approval import approve_evaluation
from app.core.evaluations.services.approval import reject_evaluation
from app.core.evaluations.services.assignments import assign_model
from app.core.evaluations.services.assignments import get_assignment
from app.core.evaluations.services.assignments import get_restorable_assignment
from app.core.evaluations.services.assignments import list_evaluation_models
from app.core.evaluations.services.assignments import restore_assignment
from app.core.evaluations.services.assignments import soft_delete_assignment
from app.core.evaluations.services.assignments import update_assignment
from app.core.evaluations.services.evaluations import authorize_evaluation_mutation
from app.core.evaluations.services.evaluations import create_evaluation
from app.core.evaluations.services.evaluations import duplicate_evaluation
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import get_restorable_evaluation
from app.core.evaluations.services.evaluations import list_evaluations
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.evaluations.services.evaluations import restore_evaluation
from app.core.evaluations.services.evaluations import soft_delete_evaluation
from app.core.evaluations.services.evaluations import update_evaluation
from app.core.evaluations.services.group_models import assert_model_assignable_to_group
from app.core.evaluations.services.tag_keys import add_tag_key
from app.core.evaluations.services.tag_keys import list_tag_keys
from app.core.evaluations.services.tag_keys import remove_tag_key
from app.core.exceptions import ForbiddenError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_GROUP_NOT_FOUND = problem_response("Evaluation group does not exist.")
_EVALUATION_NOT_FOUND = problem_response("Evaluation does not exist.")
_MODEL_NOT_FOUND = problem_response("Evaluation or model does not exist.")
_ASSIGNMENT_NOT_FOUND = problem_response("Evaluation or assignment does not exist.")
_CONFLICT = problem_response("Model is already assigned to this evaluation.")
_MODEL_NOT_ALLOWED = problem_response("Model is not in the evaluation group's allowed-model subset.")
_INVALID_STATE = problem_response("Evaluation is not awaiting review.")
_GROUP_NOT_ACCEPTING = problem_response("Evaluation group is not approved or published.")
_UNKNOWN_LICENSE = problem_response("The data_license_id does not reference a live licence.")
_EVALUATION_NOT_RESTORABLE = problem_response(
    "No restorable evaluation with this id: never deleted, deleted longer than the restore window ago, "
    "deleted by another user, or its parent group is soft-deleted."
)
_ASSIGNMENT_NOT_RESTORABLE = problem_response(
    "No restorable assignment with this id in this evaluation: never unassigned, unassigned longer than "
    "the restore window ago, or its model has since been deleted."
)


def _evaluation_snapshot(evaluation: Evaluation) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after (never whole rows)."""
    return {
        "title": evaluation.title,
        "description": evaluation.description,
        "status": str(evaluation.status),
        "mask_models_enabled": evaluation.mask_models_enabled,
        "tags_enabled": evaluation.tags_enabled,
        "tags_restricted": evaluation.tags_restricted,
        "data_license_id": str(evaluation.data_license_id) if evaluation.data_license_id else None,
        "cover_image": evaluation.cover_image,
    }


def _assignment_snapshot(assignment: EvaluationAiModel) -> dict[str, object]:
    """Curated snapshot of a model assignment — model id, display mask, inference-param overrides (no secrets)."""
    return {
        "model_id": str(assignment.model_id),
        "model_display_mask": assignment.model_display_mask,
        # copy, not alias: `parameters` is a shared mutable JSONB dict on the row, so the
        # before-snapshot must not track later mutations (the diff would then read empty).
        "parameters": dict(assignment.parameters),
    }


@router.get(
    "",
    response_model=Page[EvaluationResponse],
    status_code=status.HTTP_200_OK,
    summary="List evaluations",
    description=(
        "Return a paginated list of evaluations visible to the caller, optionally filtered by group, status, "
        "or a title/description search. An evaluation is visible when its parent group is public or one the caller "
        "is a member of; holders of `evaluation_groups:manage` see every evaluation. Each evaluation carries its "
        "assigned models; when an evaluation masks models, their identifying fields are withheld and the display "
        "mask is surfaced as the name."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_evaluations_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))],
    pagination: PaginationDep,
    filters: EvaluationFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: Annotated[
        EvaluationOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "-created_at",
) -> Page[EvaluationResponse]:
    """List evaluations visible to the caller.

    An evaluation is visible when its parent group is `public` or one the caller
    is a member of; holders of `evaluation_groups:manage` see every evaluation.

    `deleted=true` needs `evaluations:delete` and serves only tombstones the caller
    can act on — their own deletes, or every group's with `evaluation_groups:manage`.
    This list spans groups, so it cannot run the per-group write gate the restore
    itself applies; scoping by deleter keeps it from listing rows whose restore
    would 403.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission, or asked for
      `deleted=true` without `evaluations:delete`.
    """
    assert_may_list_deleted(filters.deleted, caller, Permission.EVALUATIONS_DELETE, entity="evaluations")
    evaluations, total = await list_evaluations(
        db,
        caller_id=caller.id,
        can_manage=Permission.EVALUATION_GROUPS_MANAGE in caller.permissions,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    licenses = await resolve_effective_licenses(db, [evaluation.id for evaluation in evaluations])
    return Page[EvaluationResponse](
        items=[
            EvaluationResponse.from_model(evaluation, effective_license=licenses[evaluation.id])
            for evaluation in evaluations
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "",
    response_model=EvaluationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create evaluation",
    description=(
        "Create a new evaluation under a group the caller may write — an in-group role granting "
        "`evaluations:create` (the creator holds `owner` by default), or any group with `evaluation_groups:manage`. "
        "The group must be `approved` or `published`; any other lifecycle state rejects the create with a 409. "
        "The evaluation always starts in `new` — lifecycle transitions are made through dedicated endpoints, so "
        "`status` is not part of this payload."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _UNKNOWN_LICENSE,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _GROUP_NOT_FOUND,
        status.HTTP_409_CONFLICT: _GROUP_NOT_ACCEPTING,
    },
)
@transactional
async def create_evaluation_endpoint(
    payload: EvaluationCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_CREATE))],
    db: DbSession,
    response: Response,
) -> EvaluationResponse:
    """Create a draft evaluation under a group the caller may write.

    Adding evaluations needs an in-group role granting `evaluations:create` (the
    creator holds `owner` by default), unless the caller holds
    `evaluation_groups:manage`. A non-public group the caller can't see reads as
    missing (404) rather than forbidden. The group must be `approved` or
    `published` — every other lifecycle state (pre-approval, rejected, or
    finished) rejects the create, with no admin bypass.

    ### Errors

    * **400 Bad Request** — `data_license_id` is set but doesn't reference a live licence.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:create` permission, or can
      see the parent group but lacks write access to it.
    * **404 Not Found** — the parent group does not exist or is not visible to the caller.
    * **409 Conflict** — the parent group is not `approved` or `published`.
    """
    evaluation = await create_evaluation(
        db,
        title=payload.title,
        description=payload.description,
        evaluation_group_id=payload.evaluation_group_id,
        created_by_id=caller.id,
        can_manage=Permission.EVALUATION_GROUPS_MANAGE in caller.permissions,
        cover_image=payload.cover_image,
        mask_models_enabled=payload.mask_models_enabled,
        tags_enabled=payload.tags_enabled,
        tags_restricted=payload.tags_restricted,
        data_license_id=payload.data_license_id,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_CREATE,
        object_type="evaluation",
        object_id=evaluation.id,
        after=_evaluation_snapshot(evaluation),
    )
    response.headers["Location"] = f"/api/v1/evaluations/{evaluation.id}"
    return EvaluationResponse.from_model(
        evaluation, effective_license=await resolve_effective_license(db, evaluation.id)
    )


@router.post(
    "/{evaluation_id}/duplicate",
    response_model=EvaluationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate evaluation",
    description=(
        "Copy an evaluation into a new one in the same group, owned by the caller, starting in `new`. With "
        "`include_children=true` its model assignments, scenarios, and tasks are cloned too (runtime data — "
        "conversations, flags, reviews — and API keys are never copied); the default clones the evaluation only. "
        "The admin tag schema (both tag flags and the allowed keys) always travels with the copy, "
        "children or not, so a duplicate can't silently drop the restriction. "
        "The group must be `approved` or `published` — duplicating follows the same lifecycle gate as a create. "
        "Requires `evaluations:create`, and the caller must hold write access on the source — an in-group role "
        "granting `evaluations:create` (the creator holds `owner`), or the `evaluation_groups:manage` break-glass. "
        "Merely being able to see the source is not enough."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
        status.HTTP_409_CONFLICT: _GROUP_NOT_ACCEPTING,
    },
)
@transactional
async def duplicate_evaluation_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_CREATE))],
    db: DbSession,
    response: Response,
    include_children: Annotated[
        bool,
        Query(description="Also deep-copy model assignments, scenarios and tasks (the tag schema copies either way)."),
    ] = False,
) -> EvaluationResponse:
    """Duplicate an evaluation into the same group as a new `new` evaluation.

    Copies the evaluation into a fresh row owned by the caller; with
    `include_children` it also clones the model-assignments/scenarios/tasks.
    Duplicating needs write access on the source (the same object-scope gate as a
    create): an in-group role granting `evaluations:create` — the creator holds
    `owner` — or the `evaluation_groups:manage` break-glass. An evaluation the caller
    can't see reads as 404, a visible one they lack write access on as 403. The copy
    lands in the source's own group, so the group must be `approved` or `published` —
    the same lifecycle gate as a create, with no admin bypass. Runtime data and API
    keys are never copied.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:create`, or lacks write access on the source.
    * **404 Not Found** — the source evaluation does not exist or is not visible to the caller.
    * **409 Conflict** — the parent group is not `approved` or `published`.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    evaluation = await duplicate_evaluation(
        db,
        source_evaluation_id=evaluation_id,
        caller_id=caller.id,
        can_manage=can_manage,
        include_children=include_children,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_DUPLICATE,
        object_type="evaluation",
        object_id=evaluation.id,
        after=_evaluation_snapshot(evaluation),
        context={"source_evaluation_id": str(evaluation_id)},
    )
    response.headers["Location"] = f"/api/v1/evaluations/{evaluation.id}"
    return EvaluationResponse.from_model(
        evaluation, effective_license=await resolve_effective_license(db, evaluation.id)
    )


@router.get(
    "/{evaluation_id}",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get evaluation",
    description=(
        "Fetch one evaluation with its assigned models. When the evaluation masks models, each model's "
        "identifying fields are withheld and the display mask is surfaced as the name."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
    },
)
async def get_evaluation_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))],
    db: DbSession,
) -> EvaluationResponse:
    """Fetch one evaluation visible to the caller.

    An evaluation is visible when its parent group is `public` or one the caller
    is a member of; holders of `evaluation_groups:manage` resolve any evaluation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission.
    * **404 Not Found** — no such evaluation, or its group is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    evaluation = await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage)
    return EvaluationResponse.from_model(
        evaluation, effective_license=await resolve_effective_license(db, evaluation.id)
    )


@router.patch(
    "/{evaluation_id}",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update evaluation",
    description=(
        "Partially update an evaluation under a group the caller owns (or any group, with "
        "`evaluation_groups:manage`). Omitted fields are left unchanged; explicit `null` clears "
        "`description` / `cover_image`. Lifecycle (`status`) and group membership are not editable here."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _UNKNOWN_LICENSE,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
    },
)
@transactional
async def update_evaluation_endpoint(
    evaluation_id: UUID,
    payload: EvaluationUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
) -> EvaluationResponse:
    """Partially update an evaluation.

    Editing needs an in-group role granting `evaluations:update` (e.g. the
    in-group `owner`), unless the caller holds `evaluation_groups:manage`. An
    evaluation under a non-public group the caller can't see reads as missing
    (404) rather than forbidden.

    ### Errors

    * **400 Bad Request** — `data_license_id` is set but doesn't reference a live licence.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:update` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — no such evaluation, or its group is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    evaluation = await get_evaluation(db, evaluation_id, for_update=True)
    before = _evaluation_snapshot(evaluation)
    updated = await update_evaluation(db, evaluation, payload)
    diff_before, diff_after = changed_fields(before, _evaluation_snapshot(updated))
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_UPDATE,
        object_type="evaluation",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return EvaluationResponse.from_model(updated, effective_license=await resolve_effective_license(db, updated.id))


@router.delete(
    "/{evaluation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete evaluation",
    description=(
        "Soft-delete an evaluation (sets `deleted_at`); subsequent reads exclude it. Allowed with an in-group "
        "role granting `evaluations:delete`, or for any caller holding `evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
    },
)
@transactional
async def delete_evaluation_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_DELETE))],
    db: DbSession,
) -> Response:
    """Soft-delete an evaluation.

    Deleting needs an in-group role granting `evaluations:delete` (e.g. the
    in-group `owner`), unless the caller holds `evaluation_groups:manage`. An
    evaluation under a non-public group the caller can't see reads as missing
    (404) rather than forbidden. Conversations run against the evaluation become
    invisible through the parent-visibility scope (no separate tombstone).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:delete` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — no such evaluation, or its group is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_DELETE
    )
    evaluation = await get_evaluation(db, evaluation_id, for_update=True)
    before = _evaluation_snapshot(evaluation)
    await soft_delete_evaluation(db, evaluation, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_DELETE,
        object_type="evaluation",
        object_id=evaluation_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{evaluation_id}/models",
    response_model=EvaluationAiModelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign model to evaluation",
    description=(
        "Attach an AI model to an evaluation, optionally with a display mask and per-assignment "
        "inference-parameter overrides. A model can be assigned to a given evaluation only once. "
        "Allowed with an in-group role granting `evaluations:update`, or for any caller holding "
        "`evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _MODEL_NOT_ALLOWED,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _MODEL_NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def assign_model_endpoint(
    evaluation_id: UUID,
    payload: EvaluationAiModelAssign,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
    response: Response,
) -> EvaluationAiModelResponse:
    """Assign a model to an evaluation.

    Changing an evaluation's model assignments needs an in-group role granting
    `evaluations:update`, unless the caller holds `evaluation_groups:manage`. An
    evaluation under a non-public group the caller can't see reads as missing (404).

    ### Errors

    * **400 Bad Request** — the model is not in the parent group's allowed-model subset
      (an empty subset allows nothing).
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:update` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — the evaluation (or its group, to the caller) or the model does not exist.
    * **409 Conflict** — the model is already assigned to this evaluation.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group_id, _ = await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    # The model must be one the parent group permits (its allowed-model subset).
    await assert_model_assignable_to_group(db, group_id=group_id, model_id=payload.model_id)
    assignment = await assign_model(
        db,
        evaluation_id=evaluation_id,
        model_id=payload.model_id,
        model_display_mask=payload.model_display_mask,
        parameters=dump_inference_params(payload.parameters),
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_MODEL_ASSIGN,
        object_type="evaluation_ai_model",
        object_id=assignment.id,
        after=_assignment_snapshot(assignment),
        context={"evaluation_id": str(evaluation_id)},
    )
    response.headers["Location"] = f"/api/v1/evaluations/{evaluation_id}/models/{assignment.id}"
    return EvaluationAiModelResponse.from_model(assignment)


@router.get(
    "/{evaluation_id}/tag-keys",
    response_model=list[EvaluationTagKeyResponse],
    status_code=status.HTTP_200_OK,
    summary="List allowed conversation-tag keys",
    # Flat, not a `Page[T]`: the set is capped at the same ceiling as a conversation's tag map, so it
    # cannot outgrow one response, and the write path reads it whole on every create / PATCH / send.
    description=(
        "The evaluation's admin-defined allowed conversation-tag keys — at most "
        f"{MAX_TAGS}, the cap a single conversation's tag map has. Enforced only while the "
        "evaluation's `tags_restricted` flag is set — then conversation tag keys must all be in this "
        "set (an empty set forbids every tag); while the flag is off the keys are inert and tags are "
        "free-form."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
    },
)
async def list_tag_keys_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))],
    db: DbSession,
) -> list[EvaluationTagKeyResponse]:
    """List the evaluation's allowed conversation-tag keys (scoped to the caller's visibility)."""
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage, with_models=False)
    rows = await list_tag_keys(db, evaluation_id)
    return [EvaluationTagKeyResponse.from_model(row) for row in rows]


@router.post(
    "/{evaluation_id}/tag-keys",
    response_model=EvaluationTagKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Allow a conversation-tag key",
    description=(
        "Add a key to the evaluation's allowed conversation-tag set. Needs an in-group role granting "
        f"`evaluations:update`, or `evaluation_groups:manage`. The set is capped at {MAX_TAGS} keys."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response(
            "Tag key is already allowed for this evaluation, or the evaluation is at the key cap."
        ),
    },
)
@transactional
async def add_tag_key_endpoint(
    evaluation_id: UUID,
    payload: EvaluationTagKeyCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
    response: Response,
) -> EvaluationTagKeyResponse:
    """Allow a conversation-tag key on the evaluation."""
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    row = await add_tag_key(db, evaluation_id=evaluation_id, key=payload.key)
    response.headers["Location"] = f"/api/v1/evaluations/{evaluation_id}/tag-keys/{row.key}"
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_TAG_KEY_ADD,
        object_type="evaluation_tag_key",
        object_id=row.id,
        after={"key": row.key},
        context={"evaluation_id": str(evaluation_id)},
    )
    return EvaluationTagKeyResponse.from_model(row)


@router.delete(
    "/{evaluation_id}/tag-keys/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disallow a conversation-tag key",
    description=(
        "Remove a key from the evaluation's allowed conversation-tag set. Existing conversation tags "
        "are left untouched. Needs `evaluations:update` or `evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation or tag key does not exist."),
    },
)
@transactional
async def remove_tag_key_endpoint(
    evaluation_id: UUID,
    key: str,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
) -> None:
    """Disallow a conversation-tag key on the evaluation."""
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    removed = await remove_tag_key(db, evaluation_id=evaluation_id, key=key, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_TAG_KEY_REMOVE,
        object_type="evaluation_tag_key",
        object_id=removed.id,
        before={"key": removed.key},
        context={"evaluation_id": str(evaluation_id)},
    )


@router.get(
    "/{evaluation_id}/models",
    response_model=Page[EvaluationAiModelView],
    status_code=status.HTTP_200_OK,
    summary="List models assigned to an evaluation",
    description=(
        "Return a paginated list of the models assigned to an evaluation, with optional name search and "
        "ordering. When the evaluation masks models, each model's identity is withheld and its display mask "
        "is surfaced as the name; search and name ordering then run against the display mask. "
        "`deleted=true` serves the assignments unassigned inside the restore window instead — the set "
        "`POST …/{assignment_id}/restore` can act on, so it requires exactly what the unassign and the "
        "restore do: the `evaluations:update` permission **and** write access to the parent group."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGNMENT_NOT_FOUND,
    },
)
async def list_evaluation_models_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_READ))],
    pagination: PaginationDep,
    filters: EvaluationAiModelFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: Annotated[
        EvaluationAiModelOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "name",
) -> Page[EvaluationAiModelView]:
    """List the models assigned to an evaluation visible to the caller.

    Visibility matches the evaluation detail read: the parent group must be
    `public` or one the caller is a member of. When the evaluation masks models, the
    `search` filter and `name` ordering operate on each assignment's display
    mask rather than the real model name.

    `deleted=true` is the restore surface, so it is authorized as a write rather than
    a read — **both** halves the unassign and the restore require: the global
    `evaluations:update` permission *and* write access to the parent group. Without the
    global check an in-group `owner` holding only `evaluations:read` globally would list
    tombstones and then get 403 from every Restore. Reading the live list needs neither.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:read` permission, or asked for
      `deleted=true` without global `evaluations:update` or without write access to the
      parent group.
    * **404 Not Found** — no such evaluation, or its group is not visible to the caller.
    """
    if filters.deleted and Permission.EVALUATIONS_UPDATE not in caller.permissions:
        raise ForbiddenError("Listing unassigned models requires the 'evaluations:update' permission.")
    if filters.deleted:
        await authorize_evaluation_mutation(
            db,
            evaluation_id,
            caller_id=caller.id,
            can_manage=Permission.EVALUATION_GROUPS_MANAGE in caller.permissions,
            permission=Permission.EVALUATIONS_UPDATE,
        )
    items, total, masked = await list_evaluation_models(
        db,
        evaluation_id=evaluation_id,
        caller_id=caller.id,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[EvaluationAiModelView](
        items=[EvaluationAiModelView.from_assignment(assignment, masked=masked) for assignment in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{evaluation_id}/models/{assignment_id}",
    response_model=EvaluationAiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Get model assignment",
    description=(
        "Fetch one model assignment within an evaluation, addressed by the assignment id, with its model identity "
        "unmasked — the full payload backing the edit form. Restricted to callers holding an in-group role granting "
        "`evaluations:update` (or `evaluation_groups:manage`); read-only callers see model identities only through "
        "the masked list. "
        "An evaluation under a non-public group the caller can't see reads as missing (404)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGNMENT_NOT_FOUND,
    },
)
async def get_assignment_endpoint(
    evaluation_id: UUID,
    assignment_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
) -> EvaluationAiModelResponse:
    """Fetch one model assignment, unmasked — the edit-form payload.

    Intentionally an edit-scoped read, gated on `evaluations:update`: a model
    assignment is *configuration*, not a result, so a group member holding only a
    read-level role (`viewer` / `red_teamer`) cannot fetch the unmasked payload —
    they see model identities only through the masked list. An assignment under a
    non-public group the caller can't see reads as missing (404).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:update` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — no such assignment, or its evaluation is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    assignment = await get_assignment(db, evaluation_id, assignment_id)
    return EvaluationAiModelResponse.from_model(assignment)


@router.patch(
    "/{evaluation_id}/models/{assignment_id}",
    response_model=EvaluationAiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Update model assignment",
    description=(
        "Partially update a model assignment. Omitted fields are left unchanged; explicit `null` clears the "
        "display mask. `parameters` replaces the whole override layer rather than merging — sending `{}` clears "
        "all per-assignment overrides. The assigned model itself is not mutable — unassign and re-assign to change it. "
        "Allowed with an in-group role granting `evaluations:update`, or for any caller holding "
        "`evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGNMENT_NOT_FOUND,
    },
)
@transactional
async def update_assignment_endpoint(
    evaluation_id: UUID,
    assignment_id: UUID,
    payload: EvaluationAiModelUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
) -> EvaluationAiModelResponse:
    """Partially update a model assignment.

    Changing an evaluation's model assignments needs an in-group role granting
    `evaluations:update`, unless the caller holds `evaluation_groups:manage`. An
    evaluation under a non-public group the caller can't see reads as missing (404).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:update` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — no such assignment in this evaluation.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    assignment = await get_assignment(db, evaluation_id, assignment_id, for_update=True)
    before = _assignment_snapshot(assignment)
    data = payload.model_dump(exclude_unset=True)
    if payload.parameters is not None:
        # `exclude_unset` recurses into the nested model, so a knob set to
        # `null` would otherwise survive into JSONB; re-dump with `exclude_none`
        # to strip it — matching the assign path so storage never holds a
        # `null` knob.
        data["parameters"] = dump_inference_params(payload.parameters)
    changes = EvaluationAiModelUpdateChanges.model_validate(data)
    updated = await update_assignment(db, assignment, changes)
    diff_before, diff_after = changed_fields(before, _assignment_snapshot(updated))
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_MODEL_UPDATE,
        object_type="evaluation_ai_model",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return EvaluationAiModelResponse.from_model(updated)


@router.delete(
    "/{evaluation_id}/models/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unassign model from evaluation",
    description=(
        "Soft-delete the assignment (sets `deleted_at`), unassigning the model from the evaluation. "
        "Allowed with an in-group role granting `evaluations:update`, or for any caller holding "
        "`evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGNMENT_NOT_FOUND,
    },
)
@transactional
async def unassign_model_endpoint(
    evaluation_id: UUID,
    assignment_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
) -> Response:
    """Unassign a model from an evaluation.

    Sets `deleted_at` on the assignment; subsequent reads exclude it, and the
    same model may be re-assigned afterwards (a fresh assignment id). Returns
    `204 No Content`. Changing an evaluation's model assignments needs an in-group
    role granting `evaluations:update`, unless the caller holds
    `evaluation_groups:manage`. Conversations against this assignment are
    soft-deleted in the same transaction.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:update` permission, or can
      see the evaluation but lacks write access to the parent group.
    * **404 Not Found** — no such assignment in this evaluation.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    assignment = await get_assignment(db, evaluation_id, assignment_id, for_update=True)
    before = _assignment_snapshot(assignment)
    await soft_delete_assignment(db, assignment, by_id=caller.id)
    # Cascade: the chosen model is gone, so its conversations can no longer run.
    await soft_delete_conversations_for_assignment(db, assignment_id, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_MODEL_UNASSIGN,
        object_type="evaluation_ai_model",
        object_id=assignment_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{evaluation_id}/restore",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore evaluation",
    description=(
        "Clear a soft-deleted evaluation's `deleted_at`. Restorable for a fixed window after the "
        "delete (set per deployment), and authorized like the delete it undoes — by the user who "
        "deleted it, or by an `evaluation_groups:manage` break-glass holder. "
        "Its scenarios, tasks, model assignments and conversations come back with it — the delete "
        "never tombstoned them, they were hidden through the parent-visibility join — while anything "
        "deleted in its own right stays deleted. Not restorable once the parent **group** is "
        "soft-deleted: the row would return invisible to everyone."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_RESTORABLE,
    },
)
@transactional
async def restore_evaluation_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_DELETE))],
    db: DbSession,
    settings: SettingsDep,
) -> EvaluationResponse:
    """Restore a soft-deleted evaluation.

    Gated on `evaluations:delete` plus write access to the parent group — the same
    pair the delete required — and narrowed to the caller's own deletes unless they
    hold `evaluation_groups:manage`, exactly as the deleted listing is.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:delete`, or lacks write access to
      the parent group.
    * **404 Not Found** — no restorable evaluation: never deleted, outside the restore
      window, another user's delete, or its parent group is soft-deleted.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db,
        evaluation_id,
        caller_id=caller.id,
        can_manage=can_manage,
        permission=Permission.EVALUATIONS_DELETE,
        allow_deleted=True,
    )
    evaluation = await get_restorable_evaluation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, deleted_cutoff=restore_cutoff(settings)
    )
    restored = await restore_evaluation(db, evaluation)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_RESTORE,
        object_type="evaluation",
        object_id=evaluation_id,
        after=_evaluation_snapshot(restored),
    )
    # Re-read with the models loaded: the restore locked the bare row, and the
    # response embeds every live assignment.
    with_models = await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage)
    license_str = await resolve_effective_license(db, evaluation_id)
    return EvaluationResponse.from_model(with_models, effective_license=license_str)


@router.post(
    "/{evaluation_id}/models/{assignment_id}/restore",
    response_model=EvaluationAiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Re-instate an unassigned model",
    description=(
        "Clear a soft-deleted assignment's `deleted_at`, making the model dispatchable in this "
        "evaluation again. Restorable for a fixed window after the unassign (set per "
        "deployment). The conversations the unassign soft-deleted become restorable again alongside it "
        "— they are gated on a live assignment, not on a flag of their own — but each still needs its "
        "own restore. Not restorable once the **model** itself has been deleted (it could never "
        "dispatch), and conflicts when the model has since been re-assigned to this evaluation."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGNMENT_NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def restore_assignment_endpoint(
    evaluation_id: UUID,
    assignment_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_UPDATE))],
    db: DbSession,
    settings: SettingsDep,
) -> EvaluationAiModelResponse:
    """Re-instate a model assignment the unassign soft-deleted.

    Authorized exactly like the unassign it undoes: an in-group role granting
    `evaluations:update`, or `evaluation_groups:manage`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluations:update`, or can see the evaluation
      but lacks write access to the parent group.
    * **404 Not Found** — no restorable assignment: never unassigned, outside the
      restore window, or its model has since been deleted.
    * **409 Conflict** — the model is already assigned to this evaluation.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    await authorize_evaluation_mutation(
        db, evaluation_id, caller_id=caller.id, can_manage=can_manage, permission=Permission.EVALUATIONS_UPDATE
    )
    assignment = await get_restorable_assignment(
        db, evaluation_id, assignment_id, deleted_cutoff=restore_cutoff(settings)
    )
    restored = await restore_assignment(db, assignment)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_MODEL_RESTORE,
        object_type="evaluation_ai_model",
        object_id=assignment_id,
        after=_assignment_snapshot(restored),
    )
    return EvaluationAiModelResponse.from_model(restored)


@router.post(
    "/{evaluation_id}/approve",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve evaluation",
    description="Approve an evaluation awaiting review. Admin-only.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def approve_evaluation_endpoint(
    evaluation_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_APPROVE))],
    db: DbSession,
) -> EvaluationResponse:
    """Approve an evaluation awaiting review.

    Moves an `under_review` evaluation to `approved` and clears any prior
    rejection reason.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:approve` permission.
    * **404 Not Found** — the evaluation does not exist.
    * **409 Conflict** — the evaluation is not awaiting review.
    """
    evaluation, previous_status = await approve_evaluation(db, evaluation_id, caller_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_APPROVE,
        object_type="evaluation",
        object_id=evaluation_id,
        before={"status": str(previous_status)},
        after={"status": str(evaluation.status)},
    )
    return EvaluationResponse.from_model(
        evaluation, effective_license=await resolve_effective_license(db, evaluation.id)
    )


@router.post(
    "/{evaluation_id}/reject",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject evaluation",
    description="Reject an evaluation awaiting review, recording the reason. Admin-only.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _EVALUATION_NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def reject_evaluation_endpoint(
    evaluation_id: UUID,
    payload: EvaluationRejectRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATIONS_APPROVE))],
    db: DbSession,
) -> EvaluationResponse:
    """Reject an evaluation awaiting review.

    Moves an `under_review` evaluation to `rejected` and stores the reason.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluations:approve` permission.
    * **404 Not Found** — the evaluation does not exist.
    * **409 Conflict** — the evaluation is not awaiting review.
    """
    evaluation, previous_status = await reject_evaluation(
        db, evaluation_id, caller_id=caller.id, rejection_reason=payload.rejection_reason
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_REJECT,
        object_type="evaluation",
        object_id=evaluation_id,
        before={"status": str(previous_status)},
        after={"status": str(evaluation.status), "rejection_reason": payload.rejection_reason},
    )
    return EvaluationResponse.from_model(
        evaluation, effective_license=await resolve_effective_license(db, evaluation.id)
    )
