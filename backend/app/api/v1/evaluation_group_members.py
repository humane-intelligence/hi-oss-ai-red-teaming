"""Evaluation-group membership endpoints — `/api/v1/evaluation-groups/{group_id}/members`.

Per-group role assignment, backed by the generic object-role layer
(`app/core/auth/object_roles/`) with `ObjectType.EVALUATION_GROUP`. Reads are
gated on group visibility; writes on the object-scope
`evaluation_groups:manage_members` permission (the in-group `owner` role or a
break-glass admin — never a member holding a lesser role, per the override rule).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.schemas import ObjectMemberCreate
from app.core.auth.object_roles.schemas import ObjectMemberResponse
from app.core.auth.object_roles.schemas import ObjectMemberUpdate
from app.core.auth.object_roles.service import add_member
from app.core.auth.object_roles.service import held_roles
from app.core.auth.object_roles.service import list_members
from app.core.auth.object_roles.service import remove_member
from app.core.auth.object_roles.service import resolve_assignable_roles
from app.core.auth.object_roles.service import set_member_roles
from app.core.auth.services.users import get_user
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.evaluations.dependencies import GroupReadDep
from app.core.evaluations.dependencies import ManageMembersDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/evaluation-groups/{group_id}/members", tags=["evaluation-groups"])

_OBJECT_TYPE = ObjectType.EVALUATION_GROUP
_SCOPE_SPEC = OBJECT_ROLE_REGISTRY[_OBJECT_TYPE]

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks permission to manage this group's members.")
_NOT_FOUND = problem_response("Evaluation group not found, or not visible to the caller.")


@router.get(
    "",
    response_model=Page[ObjectMemberResponse],
    status_code=status.HTTP_200_OK,
    summary="List group members",
    description="Return a paginated list of the group's members and the roles each holds.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the `evaluation_groups:read` permission."),
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def list_group_members_endpoint(
    context: GroupReadDep,
    pagination: PaginationDep,
    db: DbSession,
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on member email / first / last name.", max_length=320),
    ] = None,
    role_id: Annotated[UUID | None, Query(description="Narrow to members holding this role in the group.")] = None,
) -> Page[ObjectMemberResponse]:
    """List the members of an evaluation group.

    Each entry is one user with the set of roles they hold in the group. Gated
    like `GET /evaluation-groups/{id}`: the caller needs the global
    `evaluation_groups:read` permission and visibility of the group. Optional
    `search` / `role_id` narrow the list server-side (the export author picker
    passes both to page the red-teamer members).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:read` permission.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    """
    members, total = await list_members(
        db,
        _OBJECT_TYPE,
        context.group.id,
        role_id=role_id,
        search=search,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[ObjectMemberResponse](
        items=[ObjectMemberResponse.from_member(member) for member in members],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "",
    response_model=ObjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add group member",
    description="Assign a user one or more roles within the group.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("A role is unknown or not assignable within a group."),
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The user is already a member of this group."),
    },
)
@transactional
async def add_group_member_endpoint(
    payload: ObjectMemberCreate,
    context: ManageMembersDep,
    caller: CurrentUserDep,
    db: DbSession,
    response: Response,
) -> ObjectMemberResponse:
    """Add a member to an evaluation group with one or more roles.

    Held roles become the member's *entire* effective permission set on the group
    — never combined with their global permissions.

    ### Errors

    * **400 Bad Request** — `role_ids` references an unknown role, or one not assignable within a group.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group or the target user does not exist.
    * **409 Conflict** — the user is already a member of this group.
    """
    user = await get_user(db, payload.user_id)
    roles = await resolve_assignable_roles(db, _SCOPE_SPEC, payload.role_ids)
    member = await add_member(db, _OBJECT_TYPE, context.group.id, user, roles)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.MEMBER_ADD,
        object_type="evaluation_group_member",
        object_id=payload.user_id,
        after={"roles": sorted(role.name for role in member.roles)},
        context={"group_id": str(context.group.id)},
    )
    response.headers["Location"] = f"/api/v1/evaluation-groups/{context.group.id}/members/{payload.user_id}"
    return ObjectMemberResponse.from_member(member)


@router.patch(
    "/{user_id}",
    response_model=ObjectMemberResponse,
    status_code=status.HTTP_200_OK,
    summary="Replace a member's roles",
    description="Replace the full set of roles a member holds within the group.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("A role is unknown or not assignable within a group."),
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("The group does not exist, or the user is not a member."),
        status.HTTP_409_CONFLICT: problem_response("The change would drop the group's last `owner`."),
    },
)
@transactional
async def update_group_member_endpoint(
    user_id: UUID,
    payload: ObjectMemberUpdate,
    context: ManageMembersDep,
    caller: CurrentUserDep,
    db: DbSession,
) -> ObjectMemberResponse:
    """Replace a member's role set within the group wholesale.

    As with adding a member, the resulting roles are the member's entire
    effective permission set on the group.

    ### Errors

    * **400 Bad Request** — `role_ids` references an unknown role, or one not assignable within a group.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist, or the user is not a member.
    * **409 Conflict** — the new role set would drop the group's last `owner`; assign another owner first.
    """
    user = await get_user(db, user_id)
    before_roles = sorted(role.name for role in await held_roles(db, _OBJECT_TYPE, context.group.id, user_id))
    roles = await resolve_assignable_roles(db, _SCOPE_SPEC, payload.role_ids)
    member = await set_member_roles(db, _OBJECT_TYPE, context.group.id, user, roles, by_id=caller.id)
    after_roles = sorted(role.name for role in member.roles)
    # A no-op replace (same role set) writes no audit row — mirrors the group PATCH's diff guard.
    if before_roles != after_roles:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.MEMBER_SET_ROLES,
            object_type="evaluation_group_member",
            object_id=user_id,
            before={"roles": before_roles},
            after={"roles": after_roles},
            context={"group_id": str(context.group.id)},
        )
    return ObjectMemberResponse.from_member(member)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove group member",
    description="Remove a user from the group, dropping all roles they hold within it.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("The group does not exist, or the user is not a member."),
        status.HTTP_409_CONFLICT: problem_response("The user is the group's last `owner`."),
    },
)
@transactional
async def remove_group_member_endpoint(
    user_id: UUID,
    context: ManageMembersDep,
    caller: CurrentUserDep,
    db: DbSession,
) -> Response:
    """Remove a member from the group (soft-deletes every role they hold).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist, or the user is not a member.
    * **409 Conflict** — the user is the group's last `owner`; assign another owner first.
    """
    before_roles = sorted(role.name for role in await held_roles(db, _OBJECT_TYPE, context.group.id, user_id))
    await remove_member(db, _OBJECT_TYPE, context.group.id, user_id, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.MEMBER_REMOVE,
        object_type="evaluation_group_member",
        object_id=user_id,
        before={"roles": before_roles},
        context={"group_id": str(context.group.id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
