"""Evaluation-group endpoints — mounted under `/api/v1/evaluation-groups`."""

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
from app.core.auth.object_roles.schemas import ObjectMemberResponse
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.evaluations.dependencies import EvaluationGroupFiltersDep
from app.core.evaluations.dependencies import GroupReadDep
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.filters import EvaluationGroupOrderBy
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.schemas import EvaluationGroupAiModelResponse
from app.core.evaluations.schemas import EvaluationGroupCreate
from app.core.evaluations.schemas import EvaluationGroupDetailResponse
from app.core.evaluations.schemas import EvaluationGroupDraftCreate
from app.core.evaluations.schemas import EvaluationGroupRejectRequest
from app.core.evaluations.schemas import EvaluationGroupResponse
from app.core.evaluations.schemas import EvaluationGroupUpdate
from app.core.evaluations.schemas import EvaluationGroupUpdateChanges
from app.core.evaluations.services.evaluation_groups import collect_publication_blockers
from app.core.evaluations.services.evaluation_groups import create_evaluation_group
from app.core.evaluations.services.evaluation_groups import create_evaluation_group_draft
from app.core.evaluations.services.evaluation_groups import duplicate_evaluation_group
from app.core.evaluations.services.evaluation_groups import get_evaluation_group
from app.core.evaluations.services.evaluation_groups import list_evaluation_groups
from app.core.evaluations.services.evaluation_groups import resolve_group_user_permissions
from app.core.evaluations.services.evaluation_groups import update_evaluation_group
from app.core.evaluations.services.membership import join_evaluation_group
from app.core.evaluations.services.publication import approve_evaluation_group
from app.core.evaluations.services.publication import finish_evaluation_group
from app.core.evaluations.services.publication import publish_evaluation_group
from app.core.evaluations.services.publication import reject_evaluation_group
from app.core.evaluations.services.publication import request_changes_for_evaluation_group
from app.core.evaluations.services.publication import submit_evaluation_group
from app.core.exceptions import ForbiddenError
from app.core.licenses.service import get_default_license
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/evaluation-groups", tags=["evaluation-groups"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Evaluation group does not exist or is not visible to the caller.")
_BAD_REQUEST = problem_response(
    "Invalid request: bad dates (`start_date` before today on create, or `end_date` not after `start_date`), "
    "or `access_level` is `organization` without a live `organization_id`."
)
_INVALID_STATE = problem_response("Group is not in a state that allows this transition.")
_INCOMPLETE = problem_response(
    "Group is incomplete: a required field is missing (`title`, `description`, `start_date`), "
    "`start_date` is before today, dates are out of order, the `organization` invariant fails, "
    "no allowed model is assigned, or an evaluation of the group has no scenario."
)


def _group_snapshot(group: EvaluationGroup) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after (JSON-safe scalars only)."""
    return {
        "title": group.title,
        "description": group.description,
        "access_level": str(group.access_level),
        "status": str(group.status),
        "metrics_access_during": str(group.metrics_access_during),
        "metrics_access_after": str(group.metrics_access_after),
        "organization_id": str(group.organization_id) if group.organization_id else None,
        "start_date": group.start_date.isoformat() if group.start_date else None,
        "end_date": group.end_date.isoformat() if group.end_date else None,
        "data_license_id": str(group.data_license_id) if group.data_license_id else None,
    }


@router.get(
    "",
    response_model=Page[EvaluationGroupResponse],
    status_code=status.HTTP_200_OK,
    summary="List evaluation groups",
    description=(
        "Return a paginated list of evaluation groups visible to the caller. "
        "Holders of `evaluation_groups:manage` may pass `all_groups=true` to list every group."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_evaluation_groups_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_READ))],
    pagination: PaginationDep,
    filters: EvaluationGroupFiltersDep,
    db: DbSession,
    order_by: Annotated[
        EvaluationGroupOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "-created_at",
) -> Page[EvaluationGroupResponse]:
    """List evaluation groups visible to the caller.

    A group is visible when it is `public` or one the caller holds an in-group
    role in (the creator holds `owner`, so they see their own groups).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:read`, or requested
      `all_groups=true` without `evaluation_groups:manage`.
    """
    if filters.all_groups and Permission.EVALUATION_GROUPS_MANAGE not in caller.permissions:
        raise ForbiddenError("Listing all evaluation groups requires the 'evaluation_groups:manage' permission.")
    groups, total = await list_evaluation_groups(
        db,
        caller_id=caller.id,
        show_all=filters.all_groups,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    default_license = await get_default_license(db)
    return Page[EvaluationGroupResponse](
        items=[EvaluationGroupResponse.from_model(group, default_license=default_license) for group in groups],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create evaluation group",
    description=(
        "Create a complete evaluation group, submitted for review; the creator is granted the in-group `owner` "
        "role. The group starts in `pending_approval`. To save partial progress instead, use `POST /draft`. "
        "A non-admin may set `organization_id` only to their own organization; holders of "
        "`evaluation_groups:manage` may assign any live organization. An optional `data_license_id` "
        "(a licence id from `GET /licenses`) sets the group's data license; omitted, an `invitation_only` "
        "group gets `No license` and any other access level inherits the platform default."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
@transactional
async def create_evaluation_group_endpoint(
    payload: EvaluationGroupCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_CREATE))],
    db: DbSession,
    response: Response,
) -> EvaluationGroupResponse:
    """Create a new evaluation group.

    The caller is recorded as `created_by_id` and granted the in-group `owner`
    role (their object authority — `created_by_id` itself confers none); the group
    is created in `pending_approval` (submitted for review — partial progress goes
    through `POST /draft` instead). `start_date` must not be before today and, when
    set, `end_date` must be after `start_date`.

    ### Errors

    * **400 Bad Request** — `start_date` is before today, `end_date` is set but
      not after `start_date`, or `organization_id` does not reference a live
      organization.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:create` permission,
      or sets `organization_id` to an organization they do not belong to (without
      `evaluation_groups:manage`).
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group = await create_evaluation_group(
        db,
        caller_id=caller.id,
        title=payload.title,
        description=payload.description,
        access_level=payload.access_level,
        metrics_access_during=payload.metrics_access_during,
        metrics_access_after=payload.metrics_access_after,
        organization_id=payload.organization_id,
        can_manage=can_manage,
        start_date=payload.start_date,
        end_date=payload.end_date,
        data_license_id=payload.data_license_id,
        data_license_set="data_license_id" in payload.model_fields_set,
        allowed_model_ids=payload.allowed_model_ids,
        status=PublicationStatus.PENDING_APPROVAL,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_CREATE,
        object_type="evaluation_group",
        object_id=group.id,
        after=_group_snapshot(group),
        context={"allowed_model_ids": sorted(str(model_id) for model_id in payload.allowed_model_ids)},
    )
    response.headers["Location"] = f"/api/v1/evaluation-groups/{group.id}"
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/draft",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a draft evaluation group",
    description=(
        "Save partial progress: create a `draft` group with only a `title` required. The completeness checks the "
        "full create enforces (required fields, `start_date` not before today, the `organization` invariant) are "
        "skipped here and re-imposed when the draft is submitted for approval (`POST /{id}/submit`): editing the "
        "draft leaves the gaps, but submitting an incomplete group is rejected (400). Tenant isolation still "
        "applies — a non-admin may set "
        "`organization_id` only to their own organization (`evaluation_groups:manage` lifts it). The creator is "
        "granted the in-group `owner` role. An omitted `data_license_id` is derived from `access_level` exactly "
        "as on the full create — and `access_level` itself defaults to `invitation_only` here, so a title-only "
        "draft carries `No license`; send an explicit `null` to inherit the platform default instead."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
@transactional
async def create_evaluation_group_draft_endpoint(
    payload: EvaluationGroupDraftCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_CREATE))],
    db: DbSession,
    response: Response,
) -> EvaluationGroupResponse:
    """Save a partial-draft evaluation group.

    Only `title` is required; the other fields are optional and the group is
    created in `draft`. The full-create completeness checks are skipped here and
    re-imposed at submit (`POST /{id}/submit` rejects an incomplete group with 400) —
    this just persists progress so the caller can finish later via the edit form.
    Tenant isolation is not deferred: a set `organization_id` must be the caller's own
    org unless they hold the `evaluation_groups:manage` break-glass.

    ### Errors

    * **400 Bad Request** — both dates are set and `end_date` is not after `start_date`.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:create`, or sets
      `organization_id` to an org they do not belong to without the manage break-glass.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group = await create_evaluation_group_draft(
        db,
        caller_id=caller.id,
        title=payload.title,
        description=payload.description,
        access_level=payload.access_level,
        metrics_access_during=payload.metrics_access_during,
        metrics_access_after=payload.metrics_access_after,
        organization_id=payload.organization_id,
        can_manage=can_manage,
        start_date=payload.start_date,
        end_date=payload.end_date,
        data_license_id=payload.data_license_id,
        data_license_set="data_license_id" in payload.model_fields_set,
        allowed_model_ids=payload.allowed_model_ids,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_DRAFT,
        object_type="evaluation_group",
        object_id=group.id,
        after=_group_snapshot(group),
    )
    response.headers["Location"] = f"/api/v1/evaluation-groups/{group.id}"
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/duplicate",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate evaluation group",
    description=(
        "Copy a group into a new `draft` owned by the caller. With `include_children=true` its evaluations, "
        "scenarios, tasks, and model assignments are cloned too (runtime data — conversations, flags, reviews — "
        "and API keys are never copied); the default clones the group only. Requires `evaluation_groups:create`, "
        "and the caller must hold write access on the source — an in-group role granting `evaluation_groups:update` "
        "(the creator holds `owner`), or the `evaluation_groups:manage` break-glass. Merely being able to see the "
        "source is not enough."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def duplicate_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_CREATE))],
    db: DbSession,
    response: Response,
    include_children: Annotated[
        bool,
        Query(description="Also deep-copy the group's evaluations, scenarios, tasks, and model assignments."),
    ] = False,
) -> EvaluationGroupResponse:
    """Duplicate an evaluation group as a new draft.

    Copies the group into a fresh `draft` owned by the caller; with
    `include_children` it also clones the evaluations/scenarios/tasks/model-assignments.
    Duplicating needs write access on the source (the same owner-or-manage gate as a
    group edit): an in-group role granting `evaluation_groups:update` — the creator
    holds `owner` — or the `evaluation_groups:manage` break-glass. A group the caller
    can't see reads as 404, a visible one they lack write access on as 403. Runtime
    data and API keys are never copied.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:create`, or lacks write access on the source.
    * **404 Not Found** — the source group does not exist or is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group = await duplicate_evaluation_group(
        db,
        source_group_id=group_id,
        caller_id=caller.id,
        can_manage=can_manage,
        include_children=include_children,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_DUPLICATE,
        object_type="evaluation_group",
        object_id=group.id,
        after=_group_snapshot(group),
        context={"source_group_id": str(group_id)},
    )
    response.headers["Location"] = f"/api/v1/evaluation-groups/{group.id}"
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.get(
    "/{group_id}",
    response_model=EvaluationGroupDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get evaluation group",
    description=(
        "Fetch one evaluation group by id, if it is visible to the caller. Embeds full child evaluations, "
        "the caller's `user_permissions` on the group (their effective in-group authority, for UI gating) "
        "and `publication_blockers` — what the group's next lifecycle step (submit or publish) would refuse, "
        "empty when it is ready."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_READ))],
    db: DbSession,
) -> EvaluationGroupDetailResponse:
    """Fetch one evaluation group by id.

    Visibility matches the list endpoint: a group is returned only when it is
    `public` or one the caller holds an in-group role in (the creator holds
    `owner`); otherwise it reads as missing (404) so the endpoint never leaks the
    existence of a private group. Holders of `evaluation_groups:manage` see any
    group regardless of access level.

    Unlike list/create/update, this response embeds the group's full child
    evaluations (models included, masking applied), ordered by creation, plus the
    caller's `user_permissions` — their effective object-scope authority on this
    group (the in-group roles they hold, or the full set under the
    `evaluation_groups:manage` break-glass) — so the UI can gate group actions
    without re-deriving them. It also embeds the group's `allowed_models` subset for
    a caller with `models:read` — held globally or via an in-group role (the `owner`
    role grants it) — and `null` otherwise (hidden, not merely empty).

    `publication_blockers` is the readiness list for the group's next lifecycle step:
    the submit gate's gaps while it is `draft` / `changes_requested`, the publish
    gate's while it is `approved`, empty otherwise. Collected from the very functions
    the transitions raise from, so the panel a UI renders and the 400 the click would
    return are the same sentences. Not permission-gated — the list describes the group,
    not the caller, and whoever may read the group may act on it or ask its owner to.
    One deliberate seam: the allowed-model entry tells a caller without `models:read`
    whether that (otherwise hidden) subset is empty — one bit of shape, no identities.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:read` permission.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group = await get_evaluation_group(
        db,
        group_id,
        caller_id=caller.id,
        can_manage=can_manage,
        with_evaluations=True,
        with_allowed_models=True,
    )
    user_permissions = await resolve_group_user_permissions(db, caller, group)
    default_license = await get_default_license(db)
    # The allowed-model subset is `models:read`-gated — hidden (null), not merely
    # empty, for callers without it. `models:read` counts whether held globally or
    # via an in-group role (the `owner` role grants it), so it rides `user_permissions`.
    models_readable = Permission.MODELS_READ in caller.permissions or Permission.MODELS_READ.value in user_permissions
    allowed_models = (
        [
            EvaluationGroupAiModelResponse.from_row(row)
            for row in sorted(group.allowed_models, key=lambda r: r.ai_model.name)
        ]
        if models_readable
        else None
    )
    return EvaluationGroupDetailResponse.from_model_with_evaluations(
        group,
        default_license=default_license,
        user_permissions=user_permissions,
        allowed_models=allowed_models,
        publication_blockers=await collect_publication_blockers(db, group),
    )


@router.patch(
    "/{group_id}",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Update evaluation group",
    description=(
        "Partially update an evaluation group. Authorized like publish/finish: an in-group role granting "
        "`evaluation_groups:update` (the creator holds `owner` by default), or a holder of "
        "`evaluation_groups:manage`. A member demoted to a lesser in-group role is denied even if they "
        "created the group. Omitted fields are left unchanged; `end_date` may be cleared with an explicit "
        "`null`, and a `null` `data_license_id` resets the group to inherit the platform default. Changing "
        "`access_level` re-derives the licence from the new level (`invitation_only` → `No license`) only "
        "when the group carries none and the field is omitted — a stored licence is never rewritten. "
        "`status` is not editable here. Assigning `organization_id` is restricted to the caller's own "
        "organization unless they hold `evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response(
            "`allowed_model_ids` drops a model still assigned to a live evaluation in the group."
        ),
    },
)
@transactional
async def update_evaluation_group_endpoint(
    group_id: UUID,
    payload: EvaluationGroupUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_UPDATE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Partially update an evaluation group.

    Authorized like the publish/finish transitions: the caller needs
    `evaluation_groups:update` via an in-group role (the creator holds `owner` by
    default), unless they hold the `evaluation_groups:manage` break-glass (which
    also lifts the visibility scope). Object authority is the held role only — a
    member demoted to a lesser in-group role is denied even though they created
    the group (`created_by_id` confers no authority). For everyone else,
    visibility matches the get endpoint: a group that is neither `public` nor
    visible to the caller reads as missing (404) rather than forbidden, so PATCH
    never leaks the existence of a private group. Unlike create, a past
    `start_date` is allowed; the merged `start_date`/`end_date` must still be in
    order.

    ### Errors

    * **400 Bad Request** — the merged `end_date` is not after `start_date`, or
      the merged org state is invalid (`organization` access without a live
      `organization_id`).
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks write access to a visible group, or assigns
      `organization_id` to an organization they do not belong to (without
      `evaluation_groups:manage`).
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — `allowed_model_ids` drops a model still assigned to a live
      evaluation in the group.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group = await get_evaluation_group(db, group_id, caller_id=caller.id, can_manage=can_manage, for_update=True)
    before = _group_snapshot(group)
    changes = EvaluationGroupUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_evaluation_group(
        db, group=group, caller_id=caller.id, can_manage=can_manage, changes=changes
    )
    diff_before, diff_after = changed_fields(before, _group_snapshot(updated))
    # The allowed-model subset isn't a scalar column, so it never shows in the diff;
    # record the new set as context when the caller supplied it.
    models_context = (
        {"allowed_model_ids": sorted(str(model_id) for model_id in changes.allowed_model_ids)}
        if changes.allowed_model_ids is not None
        else None
    )
    if diff_before or diff_after or models_context:
        # A no-op PATCH (payload matches current state) changes nothing — skip the row
        # rather than log a misleading update with empty before/after diffs.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.EVALUATION_GROUP_UPDATE,
            object_type="evaluation_group",
            object_id=updated.id,
            before=diff_before or None,
            after=diff_after or None,
            context=models_context,
        )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(updated, default_license=default_license)


@router.post(
    "/{group_id}/submit",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit evaluation group for approval",
    description=(
        "Move a `draft` (or `changes_requested`) evaluation group to `pending_approval`. "
        "Authorized like the group edit: an in-group role granting `evaluation_groups:update` "
        "(the creator holds `owner` by default), or a holder of `evaluation_groups:manage`. "
        "Re-imposes the full-create completeness checks the draft path skipped — an incomplete "
        "group (missing required fields, `start_date` before today, out-of-order dates, a broken "
        "`organization` invariant, no allowed model, or an evaluation with no scenario) is rejected "
        "with 400, which lists every gap at once."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _INCOMPLETE,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def submit_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_UPDATE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Submit a draft (or changes-requested) evaluation group for approval.

    Authorized like the group PATCH: the caller needs `evaluation_groups:update`
    via an in-group role (the creator holds `owner` by default), unless they hold
    the `evaluation_groups:manage` break-glass. A demoted member is denied even if
    they created the group. Visibility matches the PATCH endpoint: a private group
    the caller can't see reads as missing (404), a visible one they can't write is
    forbidden (403).

    Re-imposes the completeness the draft path skipped: a group saved with gaps must
    have its required fields (`title`, `description`, `start_date`), a `start_date`
    not before today, in-order dates, a valid `organization` invariant, at least one
    allowed model, and at least one scenario on every live evaluation before it can
    enter review — otherwise 400, naming every gap in one message. The same list is
    readable ahead of the click as the group detail's `publication_blockers`.

    ### Errors

    * **400 Bad Request** — the group is incomplete (missing required fields, past
      `start_date`, out-of-order dates, an invalid `organization` invariant, no
      allowed model, or an evaluation with no scenario).
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks write access to a visible group.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — the group is not `draft` or `changes_requested`.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group, previous_status = await submit_evaluation_group(db, group_id, caller_id=caller.id, can_manage=can_manage)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_SUBMIT,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status)},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/publish",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Publish evaluation group",
    description=(
        "Move an `approved` evaluation group to `published`. Authorized like the group edit: "
        "an in-group role granting `evaluation_groups:update` (the creator holds `owner` by default), "
        "or a holder of `evaluation_groups:manage`. The group must hold at least one live evaluation, "
        "and every one of them must still carry at least one scenario — the submit-time gate is "
        "re-checked here, since a scenario may have been deleted while the group sat in review. "
        "A 400 lists every gap at once."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("The group has no evaluations, or one of them has no scenario."),
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def publish_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_UPDATE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Publish an approved evaluation group.

    Authorized like the group PATCH: the caller needs `evaluation_groups:update`
    via an in-group role (the creator holds `owner` by default), unless they hold
    the `evaluation_groups:manage` break-glass. A demoted member is denied even if
    they created the group. Visibility matches the PATCH endpoint: a private group
    the caller can't see reads as missing (404), a visible one they can't write is
    forbidden (403).

    ### Errors

    * **400 Bad Request** — the group holds no evaluations, or one of them has no
      scenario (the submit-time gate, re-checked because scenarios can be deleted
      in review).
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks write access to a visible group.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — the group is not `approved`.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group, previous_status = await publish_evaluation_group(db, group_id, caller_id=caller.id, can_manage=can_manage)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_PUBLISH,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status)},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/finish",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Finish evaluation group",
    description=(
        "Move a `published` evaluation group to `inactive`. Authorized like the group edit: "
        "an in-group role granting `evaluation_groups:update` (the creator holds `owner` by default), "
        "or a holder of `evaluation_groups:manage`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def finish_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_UPDATE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Finish a published evaluation group.

    A pure state move — `end_date` keeps its scheduled value. Authorization
    matches the publish endpoint (an in-group role granting
    `evaluation_groups:update`, held by the creator as `owner` by default, or
    `evaluation_groups:manage`).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks write access to a visible group.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — the group is not `published`.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group, previous_status = await finish_evaluation_group(db, group_id, caller_id=caller.id, can_manage=can_manage)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_FINISH,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status)},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/approve",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve evaluation group",
    description=(
        "Move a `pending_approval` evaluation group to `approved`. Authorized like the group edit: "
        "an in-group role granting `evaluation_groups:update` (the creator holds `owner` by default), "
        "or a holder of `evaluation_groups:manage` — so an owner may approve their own group."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def approve_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_UPDATE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Approve an evaluation group awaiting approval.

    Moves a `pending_approval` group to `approved`. Authorized like the group PATCH:
    the caller needs `evaluation_groups:update` via an in-group role (the creator
    holds `owner` by default, so an owner may approve their own group), or the
    `evaluation_groups:manage` break-glass. Visibility matches the PATCH endpoint:
    a private group the caller can't see reads as missing (404), a visible one they
    can't write is forbidden (403).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks write access to a visible group.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — the group is not `pending_approval`.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    group, previous_status = await approve_evaluation_group(db, group_id, caller_id=caller.id, can_manage=can_manage)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_APPROVE,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status)},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/request-changes",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Request changes on evaluation group",
    description="Bounce a `pending_approval` evaluation group back for edits. Requires `evaluation_groups:manage`.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def request_changes_evaluation_group_endpoint(
    group_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_MANAGE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Request changes on an evaluation group awaiting approval.

    Moves a `pending_approval` group to `changes_requested` — the non-terminal
    moderation verdict, after which the owner can `submit` it again. Gated directly
    on `evaluation_groups:manage`; ownership plays no role.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage` permission.
    * **404 Not Found** — the group does not exist.
    * **409 Conflict** — the group is not `pending_approval`.
    """
    group, previous_status = await request_changes_for_evaluation_group(db, group_id, caller_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_REQUEST_CHANGES,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status)},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/reject",
    response_model=EvaluationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject evaluation group",
    description="Reject a `pending_approval` group, recording the reason. Requires `evaluation_groups:manage`.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _INVALID_STATE,
    },
)
@transactional
async def reject_evaluation_group_endpoint(
    group_id: UUID,
    payload: EvaluationGroupRejectRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.EVALUATION_GROUPS_MANAGE))],
    db: DbSession,
) -> EvaluationGroupResponse:
    """Reject an evaluation group awaiting approval.

    Moves a `pending_approval` group to `not_approved` and stores the reason. A
    moderation action gated directly on `evaluation_groups:manage` — ownership
    plays no role.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage` permission.
    * **404 Not Found** — the group does not exist.
    * **409 Conflict** — the group is not `pending_approval`.
    """
    group, previous_status = await reject_evaluation_group(
        db, group_id, caller_id=caller.id, rejection_reason=payload.rejection_reason
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_REJECT,
        object_type="evaluation_group",
        object_id=group_id,
        before={"status": str(previous_status)},
        after={"status": str(group.status), "rejection_reason": payload.rejection_reason},
    )
    default_license = await get_default_license(db)
    return EvaluationGroupResponse.from_model(group, default_license=default_license)


@router.post(
    "/{group_id}/join",
    response_model=ObjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Join evaluation group",
    description=(
        "Self-service join: add the caller to a self-joinable, `published` group as a `red_teamer`. "
        "`public` groups are open platform-wide; `organization` groups are open to members of the "
        "owning organization. `invitation_only` groups are not joinable directly — membership there "
        "comes only by owner/admin invitation."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response(
            "Caller lacks `evaluation_groups:read`, or the group is invitation-only."
        ),
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The group is not published, or the caller is already a member."),
    },
)
@transactional
async def join_evaluation_group_endpoint(
    group_id: UUID,
    context: GroupReadDep,
    caller: CurrentUserDep,
    db: DbSession,
) -> ObjectMemberResponse:
    """Join a self-joinable (`public` or `organization`), published group as a red teamer.

    Gated like `GET /evaluation-groups/{id}`: the caller needs
    `evaluation_groups:read` and visibility of the group, so an invitation-only
    group the caller can't see reads as 404 — never leaking its existence. An
    `organization` group is visible only to its own org, so only org members ever
    reach the join.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:read`, or the group is `invitation_only`.
    * **404 Not Found** — no such group, or it is not visible to the caller.
    * **409 Conflict** — the group is not `published`, or the caller is already a member.
    """
    member = await join_evaluation_group(db, context.group, caller_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EVALUATION_GROUP_JOIN,
        object_type="evaluation_group",
        object_id=context.group.id,
        after={"roles": sorted(role.name for role in member.roles), "user_id": str(caller.id)},
    )
    return ObjectMemberResponse.from_member(member)
