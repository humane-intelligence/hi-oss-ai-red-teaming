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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.schemas import ObjectMemberCreate
from app.core.auth.object_roles.schemas import ObjectMemberResponse
from app.core.auth.object_roles.schemas import ObjectMemberUpdate
from app.core.auth.object_roles.service import ObjectMember
from app.core.auth.object_roles.service import add_member
from app.core.auth.object_roles.service import held_roles
from app.core.auth.object_roles.service import list_members
from app.core.auth.object_roles.service import remove_member
from app.core.auth.object_roles.service import resolve_assignable_roles
from app.core.auth.object_roles.service import set_member_roles
from app.core.auth.object_roles.service import was_member
from app.core.auth.schemas import SessionUser
from app.core.auth.services.users import UserDeactivatedError
from app.core.auth.services.users import get_user
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.evaluations.dependencies import GroupReadDep
from app.core.evaluations.dependencies import ManageMembersDep
from app.core.evaluations.schemas import GroupMemberBulkRequest
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/evaluation-groups/{group_id}/members", tags=["evaluation-groups"])

_OBJECT_TYPE = ObjectType.EVALUATION_GROUP
_SCOPE_SPEC = OBJECT_ROLE_REGISTRY[_OBJECT_TYPE]

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks permission to manage this group's members.")
_NOT_FOUND = problem_response("Evaluation group not found, or not visible to the caller.")


async def _add_member(
    session: AsyncSession,
    group_id: UUID,
    payload: ObjectMemberCreate,
    caller: SessionUser,
    *,
    audit: bool = True,
    allow_removed_member: bool = False,
) -> ObjectMember:
    """Add one member and audit it — shared by the single and the bulk route.

    The bulk route passes `audit=False` on a dry run: the bulk contract gates every
    side effect on `dry_run`, not only on the rollback that happens to undo it. A
    deactivated account is refused, as by the group invitation, except that the single
    route passes `allow_removed_member` so Undo can put back a member removed from this
    group, whatever their account status.
    """
    user = await get_user(session, payload.user_id)
    roles = await resolve_assignable_roles(session, _SCOPE_SPEC, payload.role_ids)
    # A pending account is fine, its roles wait for activation.
    if user.status is UserStatus.INACTIVE and not (
        allow_removed_member and await was_member(session, _OBJECT_TYPE, group_id, user.id)
    ):
        raise UserDeactivatedError
    member = await add_member(session, _OBJECT_TYPE, group_id, user, roles)
    if audit:
        await record_audit(
            session,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.MEMBER_ADD,
            object_type="evaluation_group_member",
            object_id=payload.user_id,
            after={"roles": sorted(role.name for role in member.roles)},
            context={"group_id": str(group_id)},
        )
    return member


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
        status.HTTP_409_CONFLICT: problem_response(
            "The user is already a member of this group, or is deactivated and was never a member of it."
        ),
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
    — never combined with their global permissions. A deactivated user is refused,
    unless they were removed from this group before, so that Undo can put them back.

    ### Errors

    * **400 Bad Request** — `role_ids` references an unknown role, or one not assignable within a group.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group or the target user does not exist.
    * **409 Conflict** — the user is already a member of this group, or is deactivated and was never
      a member of it (`type: urn:redteam:error:user-deactivated`).
    """
    member = await _add_member(db, context.group.id, payload, caller, allow_removed_member=True)
    response.headers["Location"] = f"/api/v1/evaluation-groups/{context.group.id}/members/{payload.user_id}"
    return ObjectMemberResponse.from_member(member)


@router.post(
    "/bulk",
    response_model=BulkResponse[ObjectMemberResponse],
    status_code=status.HTTP_200_OK,
    summary="Add group members in bulk",
    description=(
        "Add several users to the group, each with their own roles, with per-row outcomes. Always returns "
        "200 OK when the envelope is well-formed — per-row failures (`400` for an unknown or non-assignable "
        "role, `404` for an unknown user, `409` for a user who is already a member or is deactivated) land "
        "inside `results[].error`, and the other rows are still added. A `user_id` may appear in one row "
        "only; a repeat rejects the whole request with 422. `dry_run=true` previews the outcomes and rolls "
        "back instead of committing."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def bulk_add_group_members_endpoint(
    payload: GroupMemberBulkRequest,
    context: ManageMembersDep,
    caller: CurrentUserDep,
    db: DbSession,
) -> BulkResponse[ObjectMemberResponse]:
    """Add members to an evaluation group in bulk, one row per user.

    Each row is the single-add body and goes through the same path, audit
    included except on a dry run, plus one rule of its own: a deactivated user
    is refused. Group visibility and `manage_members` are
    checked once for the envelope. No `@transactional` — `apply_bulk` owns the
    transaction (commit, or rollback on dry-run).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    * **422 Unprocessable Entity** — duplicate `row_key` or `user_id`, empty `rows`, or over the row cap.
    """

    async def processor(session: AsyncSession, data: ObjectMemberCreate) -> ObjectMemberResponse:
        member = await _add_member(session, context.group.id, data, caller, audit=not payload.dry_run)
        return ObjectMemberResponse.from_member(member)

    return await apply_bulk(db, payload, processor)


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
