"""Assignable roles endpoint — `/api/v1/evaluation-groups/{group_id}/assignable-roles`.

Lists the roles a manager can give a member of the group: what the member add,
role edit and invitation routes accept. Gated on the object-scope
`evaluation_groups:manage_members` permission (the in-group `owner` role or a
break-glass admin), so an in-group owner can fill a role picker without the
global `roles:read` the role catalog needs. Identity, label and the self-join
default flag only — a role's permission set stays behind `roles:read`.
"""

from fastapi import APIRouter
from fastapi import status

from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import list_assignable_roles
from app.core.auth.schemas import AssignableRoleResponse
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.evaluations.dependencies import ManageMembersDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/evaluation-groups/{group_id}/assignable-roles", tags=["evaluation-groups"])

_SCOPE_SPEC = OBJECT_ROLE_REGISTRY[ObjectType.EVALUATION_GROUP]


@router.get(
    "",
    response_model=Page[AssignableRoleResponse],
    status_code=status.HTTP_200_OK,
    summary="List roles assignable in the group",
    description=(
        "Return a paginated list of the roles a member of this group can be given — the source for "
        "`role_ids` on the group's member and invitation routes, which refuse any other role."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks permission to manage this group's members."),
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation group not found, or not visible to the caller."),
    },
)
async def list_group_assignable_roles_endpoint(
    _context: ManageMembersDep,
    pagination: PaginationDep,
    db: DbSession,
) -> Page[AssignableRoleResponse]:
    """List the roles assignable to a member of this group, ordered by slug.

    Live, active roles flagged as assignable in a group that also grant
    `evaluation_groups:read` — exactly the set the member add, role edit and
    invitation routes accept. Gated on `evaluation_groups:manage_members` (the
    in-group `owner` or a break-glass admin), not on the global `roles:read`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    """
    roles, total = await list_assignable_roles(db, _SCOPE_SPEC, limit=pagination.limit, offset=pagination.offset)
    return Page[AssignableRoleResponse](
        items=[AssignableRoleResponse.from_role(role) for role in roles],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
