"""Role catalog + management endpoints — mounted under `/api/v1/roles`."""

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
from app.core.auth.models import Role as RoleModel
from app.core.auth.roles import Permission
from app.core.auth.schemas import RoleCreate
from app.core.auth.schemas import RoleResponse
from app.core.auth.schemas import RoleUpdate
from app.core.auth.schemas import SessionUser
from app.core.auth.services.roles import create_role
from app.core.auth.services.roles import delete_role
from app.core.auth.services.roles import get_restorable_role
from app.core.auth.services.roles import get_role
from app.core.auth.services.roles import list_roles
from app.core.auth.services.roles import restore_role
from app.core.auth.services.roles import set_default_role
from app.core.auth.services.roles import set_participant_default_role
from app.core.auth.services.roles import set_role_active
from app.core.auth.services.roles import update_role
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.exceptions import ConflictError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilterAnyDeleterNewestFirst
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/roles", tags=["roles"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Role does not exist.")
_BAD_REQUEST = problem_response("Invalid permission set or edit of a system role.")
_CONFLICT = problem_response("Reserved/duplicate name, or a protected-role guard was hit.")
_NOT_RESTORABLE = problem_response(
    "No restorable role with this id: never deleted, or deleted longer than the restore window ago."
)


def _role_snapshot(role: RoleModel) -> dict:
    """Curated, non-secret audit snapshot — what an operator can actually change."""
    return {
        "name": role.name,
        "display_name": role.display_name,
        "description": role.description,
        "permissions": sorted(role.permissions),
        "is_active": role.is_active,
        "is_default": role.is_default,
        "is_participant_default": role.is_participant_default,
        "is_object_assignable": role.is_object_assignable,
    }


@router.get(
    "",
    response_model=Page[RoleResponse],
    status_code=status.HTTP_200_OK,
    summary="List roles",
    description=(
        "Return the catalog of roles — the source for `role_ids` when creating, updating, or inviting "
        "users. Active-only by default; pass `include_inactive=true` for the management view. "
        "`is_object_assignable=true` narrows to the roles assignable as in-group (object) roles — what an "
        "in-group role picker should offer, since assignment rejects anything else. Each entry carries the "
        "role's permissions and its `is_system` / `is_active` / `is_default` / `is_participant_default` / "
        "`is_object_assignable` flags. Pass `deleted=true` (requires `roles:manage`) for the catalog's "
        "tombstones inside the restore window — the set `POST /{role_id}/restore` accepts."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED, status.HTTP_403_FORBIDDEN: _FORBIDDEN},
)
async def list_roles_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_READ))],
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    include_inactive: Annotated[bool, Query(description="Include deactivated roles (management view).")] = False,
    is_object_assignable: Annotated[
        bool | None,
        Query(description="Narrow to roles assignable as in-group (object) roles. Omit for the whole catalog."),
    ] = None,
    deleted: DeletedFilterAnyDeleterNewestFirst = False,
) -> Page[RoleResponse]:
    """List the role catalog.

    `deleted=true` is the restore surface, so it takes the permission that restores
    (`roles:manage`) rather than the one that reads the catalog.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `roles:read` permission, or asked for
      `deleted=true` without `roles:manage`.
    """
    assert_may_list_deleted(deleted, caller, Permission.ROLES_MANAGE, entity="roles")
    items, total = await list_roles(
        db,
        limit=pagination.limit,
        offset=pagination.offset,
        include_inactive=include_inactive,
        is_object_assignable=is_object_assignable,
        deleted=deleted,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[RoleResponse](
        items=[RoleResponse.from_role(role) for role in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
    summary="Get role",
    description="Fetch a single role by id — active or inactive. The management view's detail source.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_role_endpoint(
    role_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_READ))],
    db: DbSession,
) -> RoleResponse:
    """Fetch a single role by id.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `roles:read` permission.
    * **404 Not Found** — no role exists with the given id.
    """
    role = await get_role(db, role_id)
    return RoleResponse.from_role(role)


@router.post(
    "",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create role",
    description="Create an operator-defined (custom) role. Always active and non-default.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_400_BAD_REQUEST: _BAD_REQUEST,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def create_role_endpoint(
    payload: RoleCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_MANAGE))],
    db: DbSession,
    response: Response,
) -> RoleResponse:
    """Create a custom role.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `roles:manage`.
    * **400 Bad Request** — an unknown or non-delegable permission.
    * **409 Conflict** — the `name` is reserved for a system role or already in use.
    """
    role = await create_role(
        db,
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        permissions=payload.permissions,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ROLE_CREATE,
        object_type="role",
        object_id=role.id,
        after=_role_snapshot(role),
    )
    response.headers["Location"] = f"/api/v1/roles/{role.id}"
    return RoleResponse.from_role(role)


@router.patch(
    "/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
    summary="Update role",
    description=(
        "Partially update a role. Custom roles accept label/description/permission edits; system roles "
        "accept only `is_active` / `is_default` / `is_participant_default`. `is_default: true` reassigns "
        "the new-user default; `is_participant_default: true` reassigns the group self-join default; "
        "neither `admin` nor `owner` may be made a default."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_400_BAD_REQUEST: _BAD_REQUEST,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def update_role_endpoint(
    role_id: UUID,
    payload: RoleUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_MANAGE))],
    db: DbSession,
) -> RoleResponse:
    """Partially update a role.

    Applies the provided fields in order: field edits (custom only, including
    `is_object_assignable`) → activation → new-user default → participant default —
    so one call can opt a custom role into in-group use and make it the participant
    default. Activation and the default reassignments honor the protected-role,
    default, eligibility (no admin/owner default), grantable-participant-default, and
    sole-role guards in the service layer.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `roles:manage`.
    * **400 Bad Request** — an unknown/non-delegable permission, or a field edit of a system role.
    * **404 Not Found** — no role exists with the given id.
    * **409 Conflict** — a protected/default/eligibility/sole-role guard blocks the change.
    """
    role = await get_role(db, role_id, for_update=True)
    before = _role_snapshot(role)
    # Keyed on what the client sent, not on non-None: `description: null` clears it.
    edits = payload.model_fields_set & {"display_name", "description", "permissions", "is_object_assignable"}
    if edits:
        await update_role(
            db,
            role,
            display_name=payload.display_name,
            description=payload.description,
            description_provided="description" in edits,
            permissions=payload.permissions,
            is_object_assignable=payload.is_object_assignable,
        )
    if payload.is_active is not None:
        await set_role_active(db, role, is_active=payload.is_active)
    if payload.is_default is not None:
        if payload.is_default:
            await set_default_role(db, role)
        elif role.is_default:
            raise ConflictError("Cannot clear the default flag directly; make another role the default instead.")
    if payload.is_participant_default is not None:
        if payload.is_participant_default:
            await set_participant_default_role(db, role)
        elif role.is_participant_default:
            raise ConflictError(
                "Cannot clear the participant default flag directly; make another role the participant default instead."
            )
    diff_before, diff_after = changed_fields(before, _role_snapshot(role))
    if diff_before or diff_after:
        # A no-op PATCH (empty body, or a payload matching current state) changes nothing —
        # skip the row rather than log a misleading update with empty before/after diffs.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.ROLE_UPDATE,
            object_type="role",
            object_id=role.id,
            before=diff_before or None,
            after=diff_after or None,
        )
    return RoleResponse.from_role(role)


@router.post(
    "/{role_id}/restore",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore a role",
    description=(
        "Clear a soft-deleted role's `deleted_at`, returning it to the catalog with its permission set "
        "and its activation state intact. Restorable for a fixed window after the delete (set per "
        "deployment) and gated exactly like the delete. Holders regain the role's permissions on their "
        "next token, as with any other grant. **System roles are refused (400)** — they are code-managed, "
        "and `syncroles` recreates the name rather than reviving the tombstone."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_400_BAD_REQUEST: problem_response("System roles cannot be restored."),
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: problem_response("A live role already holds the tombstone's name."),
    },
)
@transactional
async def restore_role_endpoint(
    role_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_MANAGE))],
    db: DbSession,
    settings: SettingsDep,
) -> RoleResponse:
    """Restore a soft-deleted role.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `roles:manage`.
    * **400 Bad Request** — the role is a system role (code-managed).
    * **404 Not Found** — no restorable role: never deleted, or deleted longer than
      the restore window ago.
    * **409 Conflict** — a live role already holds the tombstone's name.
    """
    role = await get_restorable_role(db, role_id, deleted_cutoff=restore_cutoff(settings))
    restored = await restore_role(db, role)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ROLE_RESTORE,
        object_type="role",
        object_id=role_id,
        after=_role_snapshot(restored),
    )
    return RoleResponse.from_role(restored)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete role",
    description="Soft-delete a custom role. System, default, and sole-active-role roles are blocked.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_400_BAD_REQUEST: problem_response("System roles cannot be deleted."),
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def delete_role_endpoint(
    role_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_MANAGE))],
    db: DbSession,
) -> Response:
    """Soft-delete a custom role.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `roles:manage`.
    * **400 Bad Request** — the role is a system role.
    * **404 Not Found** — no role exists with the given id.
    * **409 Conflict** — the role is the default, or some principal's only active role.
    """
    role = await get_role(db, role_id, for_update=True)
    audited = _role_snapshot(role)
    await delete_role(db, role, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ROLE_DELETE,
        object_type="role",
        object_id=role_id,
        before=audited,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
