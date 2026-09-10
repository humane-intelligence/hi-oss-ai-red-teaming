"""Notification endpoints — flat `/notifications` resource.

A notification is an in-app message delivered to one user. Strictly
owner-scoped: reads gate on `notifications:read`, marking on
`notifications:update`, and the service scopes every read/write to the caller's
own rows — one user never sees or touches another's. There is no HTTP create
path; rows are minted internally by `create_notification` when a domain event
concerns a user.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status

from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.notifications.dependencies import NotificationFiltersDep
from app.core.notifications.filters import NotificationOrderBy
from app.core.notifications.schemas import MarkNotificationsRequest
from app.core.notifications.schemas import NotificationMarkResult
from app.core.notifications.schemas import NotificationResponse
from app.core.notifications.services.notifications import get_notification
from app.core.notifications.services.notifications import list_notifications
from app.core.notifications.services.notifications import mark_notifications
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/notifications", tags=["notifications"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Notification does not exist or is not the caller's.")

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTIFICATIONS_READ))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTIFICATIONS_UPDATE))]

_OrderBy = Annotated[
    NotificationOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


@router.get(
    "",
    response_model=Page[NotificationResponse],
    status_code=status.HTTP_200_OK,
    summary="List notifications",
    description=(
        "Flat, paginated list of the caller's notifications; filter by read state and object type. "
        "Ordered newest-first. `total` reflects the filtered set — use `read=false` to read the unread count."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_notifications_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: NotificationFiltersDep,
    db: DbSession,
    order_by: _OrderBy = "-created_at",
) -> Page[NotificationResponse]:
    """List the caller's notifications.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notifications:read`.
    """
    notifications, total = await list_notifications(
        db,
        user_id=caller.id,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[NotificationResponse](
        items=[NotificationResponse.from_model(notification) for notification in notifications],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "/mark",
    response_model=NotificationMarkResult,
    status_code=status.HTTP_200_OK,
    summary="Mark notifications read/unread",
    description=(
        "Set the read state of the caller's notifications. `ids` selects which to flip; an empty `ids` "
        "marks **all** of the caller's. Only rows in the opposite state change; `updated` reports how many."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
@transactional
async def mark_notifications_endpoint(
    payload: MarkNotificationsRequest,
    caller: _UpdateCaller,
    db: DbSession,
) -> NotificationMarkResult:
    """Mark the caller's notifications read or unread.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notifications:update`.
    """
    updated = await mark_notifications(db, user_id=caller.id, ids=payload.ids, read=payload.read)
    return NotificationMarkResult(updated=updated)


@router.get(
    "/{notification_id}",
    response_model=NotificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get notification",
    description="Fetch one of the caller's notifications by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_notification_endpoint(
    notification_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> NotificationResponse:
    """Fetch one notification.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notifications:read`.
    * **404 Not Found** — no such notification owned by the caller.
    """
    notification = await get_notification(db, notification_id, user_id=caller.id)
    return NotificationResponse.from_model(notification)
