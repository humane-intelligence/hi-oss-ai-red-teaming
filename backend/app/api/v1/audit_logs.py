"""Audit-log read endpoint — admin-only, flat `/audit-logs` resource.

The append-only trail written by `record_audit` (mutations) and
`AuditAccessMiddleware` (accesses). Read-only; gated on `audit:read` (admin).
"""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status

from app.core.audit.dependencies import AuditLogFiltersDep
from app.core.audit.filters import AuditLogOrderBy
from app.core.audit.schemas import AuditLogResponse
from app.core.audit.service import list_audit_logs
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/audit-logs", tags=["audit"])

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.AUDIT_READ))]
_OrderBy = Annotated[AuditLogOrderBy, Query(description="Column to order by; prefix `-` for descending.")]


@router.get(
    "",
    response_model=Page[AuditLogResponse],
    status_code=status.HTTP_200_OK,
    summary="List audit-log entries",
    description=(
        "Admin-only, paginated view of the append-only audit log. Filter by `actor_id`, `action`, "
        "`object_type`, `object_id`, and a `created_from`/`created_to` UTC range. Most recent first."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks `audit:read`."),
    },
)
async def list_audit_logs_endpoint(
    _caller: _ReadCaller,
    pagination: PaginationDep,
    filters: AuditLogFiltersDep,
    db: DbSession,
    order_by: _OrderBy = "-created_at",
) -> Page[AuditLogResponse]:
    """List audit-log entries (admin-only).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `audit:read`.
    """
    rows, total = await list_audit_logs(
        db,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[AuditLogResponse](
        items=[AuditLogResponse.from_model(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
