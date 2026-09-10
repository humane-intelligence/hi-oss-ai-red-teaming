"""Organization endpoints — mounted under `/api/v1/organizations`.

Admin-only: organizations are the tenancy root, created and managed by platform
admins, who also assign/unassign member users. Membership is a single nullable
FK on `User`, so the members sub-resource just stamps `User.organization_id`.
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
from app.core.auth.schemas import UserResponse
from app.core.auth.services.users import get_user
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.organizations.dependencies import OrganizationFiltersDep
from app.core.organizations.filters import OrganizationOrderBy
from app.core.organizations.models import Organization
from app.core.organizations.schemas import OrganizationCreate
from app.core.organizations.schemas import OrganizationMemberAdd
from app.core.organizations.schemas import OrganizationResponse
from app.core.organizations.schemas import OrganizationUpdate
from app.core.organizations.services.organizations import OrganizationUpdateChanges
from app.core.organizations.services.organizations import assign_member
from app.core.organizations.services.organizations import create_organization
from app.core.organizations.services.organizations import get_organization
from app.core.organizations.services.organizations import get_restorable_organization
from app.core.organizations.services.organizations import list_organization_members
from app.core.organizations.services.organizations import list_organizations
from app.core.organizations.services.organizations import remove_member
from app.core.organizations.services.organizations import restore_organization
from app.core.organizations.services.organizations import soft_delete_organization
from app.core.organizations.services.organizations import update_organization
from app.core.restore import DeletedFilterAnyDeleter
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/organizations", tags=["organizations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Organization does not exist.")
_CONFLICT = problem_response("An organization with this name already exists.")
_IN_USE = problem_response("Organization is still referenced by one or more evaluation groups.")
_NOT_RESTORABLE = problem_response(
    "No restorable organization with this id: never deleted, or deleted longer than the restore window ago."
)


def _organization_snapshot(organization: Organization) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after (name + description only)."""
    return {"name": organization.name, "description": organization.description}


@router.get(
    "",
    response_model=Page[OrganizationResponse],
    status_code=status.HTTP_200_OK,
    summary="List organizations",
    description=(
        "Return a paginated slice of organizations with an optional name filter and ordering. "
        "`order_by` accepts a column name; prefix with `-` for descending order. Pass `deleted=true` "
        "(requires `organizations:delete`) for the tombstones inside the restore window — the set "
        "`POST /{organization_id}/restore` accepts."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED, status.HTTP_403_FORBIDDEN: _FORBIDDEN},
)
async def list_organizations_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_READ))],
    filters: OrganizationFiltersDep,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: Annotated[
        OrganizationOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "name",
    deleted: DeletedFilterAnyDeleter = False,
) -> Page[OrganizationResponse]:
    """List organizations.

    `deleted=true` is the restore surface, so it takes the permission that restores
    (`organizations:delete`) rather than the one that reads the list.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:read`, or asked for `deleted=true`
      without `organizations:delete`.
    """
    assert_may_list_deleted(deleted, caller, Permission.ORGANIZATIONS_DELETE, entity="organizations")
    items, total = await list_organizations(
        db,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted=deleted,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[OrganizationResponse](
        items=[OrganizationResponse.from_organization(o) for o in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create organization",
    description="Create a new organization. `name` must be unique among live rows.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def create_organization_endpoint(
    payload: OrganizationCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_CREATE))],
    db: DbSession,
    response: Response,
) -> OrganizationResponse:
    """Create a new organization.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:create`.
    * **409 Conflict** — a live organization with the same `name` exists.
    """
    organization = await create_organization(db, name=payload.name, description=payload.description)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ORGANIZATION_CREATE,
        object_type="organization",
        object_id=organization.id,
        after=_organization_snapshot(organization),
    )
    response.headers["Location"] = f"/api/v1/organizations/{organization.id}"
    return OrganizationResponse.from_organization(organization)


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get organization",
    description="Fetch one organization by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_organization_endpoint(
    organization_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_READ))],
    db: DbSession,
) -> OrganizationResponse:
    """Fetch one organization by id.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:read`.
    * **404 Not Found** — no organization exists with the given id.
    """
    organization = await get_organization(db, organization_id)
    return OrganizationResponse.from_organization(organization)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update organization",
    description="Partially update an organization. Omitted fields are left unchanged.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def update_organization_endpoint(
    organization_id: UUID,
    payload: OrganizationUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_UPDATE))],
    db: DbSession,
) -> OrganizationResponse:
    """Partially update an organization.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:update`.
    * **404 Not Found** — no organization exists with the given id.
    * **409 Conflict** — the new `name` collides with a live organization.
    """
    organization = await get_organization(db, organization_id, for_update=True)
    before = _organization_snapshot(organization)
    changes = OrganizationUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_organization(db, organization, changes)
    diff_before, diff_after = changed_fields(before, _organization_snapshot(updated))
    if diff_before or diff_after:
        # A no-op PATCH (payload matches current state) changes nothing — skip the row
        # rather than log a misleading update with empty before/after diffs.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.ORGANIZATION_UPDATE,
            object_type="organization",
            object_id=updated.id,
            before=diff_before,
            after=diff_after,
        )
    return OrganizationResponse.from_organization(updated)


@router.post(
    "/{organization_id}/restore",
    response_model=OrganizationResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore an organization",
    description=(
        "Clear a soft-deleted organization's `deleted_at`. Restorable for a fixed window after the "
        "delete (set per deployment) and gated exactly like the delete. Member users return with it — "
        "the delete leaves their `organization_id` pointing here, so reviving the row re-resolves them."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def restore_organization_endpoint(
    organization_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_DELETE))],
    db: DbSession,
    settings: SettingsDep,
) -> OrganizationResponse:
    """Restore a soft-deleted organization.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:delete`.
    * **404 Not Found** — no restorable organization: never deleted, or deleted longer
      than the restore window ago.
    * **409 Conflict** — a live organization already holds the tombstone's name.
    """
    organization = await get_restorable_organization(db, organization_id, deleted_cutoff=restore_cutoff(settings))
    restored = await restore_organization(db, organization)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ORGANIZATION_RESTORE,
        object_type="organization",
        object_id=organization_id,
        after=_organization_snapshot(restored),
    )
    return OrganizationResponse.from_organization(restored)


@router.delete(
    "/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete organization",
    description=(
        "Soft-delete an organization (sets `deleted_at`). Blocked while any live evaluation group still "
        "references the organization — reassign or clear those groups first."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _IN_USE,
    },
)
@transactional
async def delete_organization_endpoint(
    organization_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_DELETE))],
    db: DbSession,
) -> Response:
    """Soft-delete an organization.

    Blocked while any live evaluation group still references the org (409) — a
    soft-delete doesn't fire the FK `SET NULL`, so those groups would otherwise go
    dark and un-editable. Member *users* keep their `organization_id` pointing at the
    tombstone (benign — member detachment is deferred). Returns `204 No Content`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:delete`.
    * **404 Not Found** — no organization exists with the given id.
    * **409 Conflict** — a live evaluation group still references the organization.
    """
    organization = await get_organization(db, organization_id, for_update=True)
    before = _organization_snapshot(organization)
    await soft_delete_organization(db, organization, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ORGANIZATION_DELETE,
        object_type="organization",
        object_id=organization.id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{organization_id}/members",
    response_model=Page[UserResponse],
    status_code=status.HTTP_200_OK,
    summary="List organization members",
    description="Return a paginated slice of the users belonging to this organization.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def list_organization_members_endpoint(
    organization_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_READ))],
    pagination: PaginationDep,
    db: DbSession,
) -> Page[UserResponse]:
    """List the members of an organization.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:read`.
    * **404 Not Found** — no organization exists with the given id.
    """
    await get_organization(db, organization_id)
    items, total = await list_organization_members(
        db, organization_id, limit=pagination.limit, offset=pagination.offset
    )
    return Page[UserResponse](
        items=[UserResponse.from_user(u) for u in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "/{organization_id}/members",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Assign organization member",
    description="Assign a user to this organization. Moves the user from any prior organization.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("Organization or user does not exist."),
    },
)
@transactional
async def assign_organization_member_endpoint(
    organization_id: UUID,
    payload: OrganizationMemberAdd,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_MANAGE_MEMBERS))],
    db: DbSession,
) -> UserResponse:
    """Assign a user to an organization.

    A user belongs to at most one organization, so assigning moves them out of
    any prior org. Idempotent when the user is already a member.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:manage_members`.
    * **404 Not Found** — the organization or the target user does not exist.
    """
    # Lock the org so a concurrent soft-delete can't land between this read and
    # the assignment (else the user would be stamped onto a tombstoned org); the
    # `live_select` in `get_organization` then 404s if the delete won the race.
    organization = await get_organization(db, organization_id, for_update=True)
    user = await get_user(db, payload.user_id, for_update=True)
    before_org = user.organization_id
    assigned = await assign_member(db, organization, user)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ORGANIZATION_MEMBER_ADD,
        object_type="user",
        object_id=user.id,
        before={"organization_id": str(before_org) if before_org else None},
        after={"organization_id": str(assigned.organization_id) if assigned.organization_id else None},
        context={"organization_id": str(organization.id)},
    )
    return UserResponse.from_user(assigned)


@router.delete(
    "/{organization_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove organization member",
    description="Detach a user from this organization (clears their organization assignment).",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("Organization or membership does not exist."),
    },
)
@transactional
async def remove_organization_member_endpoint(
    organization_id: UUID,
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ORGANIZATIONS_MANAGE_MEMBERS))],
    db: DbSession,
) -> Response:
    """Remove a user from an organization.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `organizations:manage_members`.
    * **404 Not Found** — the organization does not exist, or the user is not a
      member of it.
    """
    organization = await get_organization(db, organization_id)
    user = await get_user(db, user_id, for_update=True)
    before_org = user.organization_id
    await remove_member(db, organization, user)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ORGANIZATION_MEMBER_REMOVE,
        object_type="user",
        object_id=user.id,
        # Symmetric with member_add: record the FK transition (org -> None), not a before-only snapshot.
        before={"organization_id": str(before_org) if before_org else None},
        after={"organization_id": None},
        context={"organization_id": str(organization.id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
