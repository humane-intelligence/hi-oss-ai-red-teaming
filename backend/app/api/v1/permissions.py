"""Permission catalog endpoint — mounted under `/api/v1/permissions`."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.auth.dependencies import require_permission
from app.core.auth.roles import NON_DELEGABLE_PERMISSIONS
from app.core.auth.roles import PERMISSION_DESCRIPTIONS
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import PaginationDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/permissions", tags=["permissions"])


class PermissionResponse(BaseModel):
    """One entry in the permission catalog — a namespaced key, its description, and delegability."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key": "evaluations:approve",
                "description": "Approve or reject evaluations.",
                "is_delegable": True,
            }
        }
    )

    key: str = Field(
        description="Namespaced permission string, `<resource>:<action>`.",
        examples=["evaluations:approve"],
    )
    description: str = Field(
        description="Human-readable purpose of the permission.",
        examples=["Approve or reject evaluations."],
    )
    is_delegable: bool = Field(
        description=(
            "Whether a custom role may carry this permission. `false` marks an elevation vector "
            "reserved for system roles — role builders should omit it, and `POST`/`PATCH /roles` reject it."
        ),
        examples=[True],
    )


# Static catalog projected from the RBAC vocabulary — sorted by key for a stable page order.
_CATALOG: list[PermissionResponse] = [
    PermissionResponse(
        key=permission.value,
        description=PERMISSION_DESCRIPTIONS[permission],
        is_delegable=permission not in NON_DELEGABLE_PERMISSIONS,
    )
    for permission in sorted(Permission, key=lambda p: p.value)
]


@router.get(
    "",
    response_model=Page[PermissionResponse],
    status_code=status.HTTP_200_OK,
    summary="List permissions",
    description=(
        "Return the catalog of every permission string with its description — the reference "
        "for building and reviewing roles. The same strings appear in `/roles`, `/auth/me`, and the JWT. "
        "Gated on `roles:read`, like `/roles`: the same admin/owner role-builder UI consumes both."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the required permission."),
    },
)
async def list_permissions_endpoint(
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.ROLES_READ))],
    pagination: PaginationDep,
) -> Page[PermissionResponse]:
    """List the permission catalog.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `roles:read` permission.
    """
    window = _CATALOG[pagination.offset : pagination.offset + pagination.limit]
    return Page[PermissionResponse](
        items=window,
        total=len(_CATALOG),
        limit=pagination.limit,
        offset=pagination.offset,
    )
