"""Notifications service — pure async functions over an `AsyncSession`.

Notifications are strictly **owner-scoped** personal data: a caller reads and
marks only their own rows (`user_id == caller_id`). There is no admin
break-glass and no HTTP create path — rows are minted by `create_notification`,
called from other services when a domain event concerns a user, and by the email
worker's own direct insert (`app/core/email/tasks.py`, sync session).
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.exceptions import NotFoundError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.filters import NotificationFilters
from app.core.notifications.filters import NotificationOrderBy
from app.core.notifications.models import Notification
from app.core.ordering import apply_order_by
from app.core.pagination import paginate


def _scope(statement: Select[tuple[Notification]], *, user_id: UUID) -> Select[tuple[Notification]]:
    """Constrain to the caller's own notifications — the owner boundary, in one place."""
    return statement.where(col(Notification.user_id) == user_id)


async def get_notification(session: AsyncSession, notification_id: UUID, *, user_id: UUID) -> Notification:
    """Fetch one live notification ``notification_id`` owned by the caller.

    A notification owned by another user reads as missing (404, no existence leak).

    Raises:
        NotFoundError: If no such notification is owned by the caller.
    """
    statement = _scope(Notification.live_select().where(col(Notification.id) == notification_id), user_id=user_id)
    notification = (await session.execute(statement)).scalar_one_or_none()
    if notification is None:
        raise NotFoundError(f"Notification {notification_id} not found.")
    return notification


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: UUID,
    filters: NotificationFilters,
    order_by: NotificationOrderBy,
    limit: int,
    offset: int,
) -> tuple[list[Notification], int]:
    """Return one page of the caller's notifications, optionally filtered by read state / object type."""
    statement = _scope(Notification.live_select(), user_id=user_id)
    if filters.read is not None:
        column = col(Notification.read_at)
        statement = statement.where(column.is_not(None) if filters.read else column.is_(None))
    if filters.object_type is not None:
        statement = statement.where(col(Notification.object_type) == filters.object_type)
    statement = apply_order_by(statement, Notification, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def mark_notifications(session: AsyncSession, *, user_id: UUID, ids: Sequence[UUID], read: bool) -> int:
    """Flip the read state of the caller's notifications; empty ``ids`` means all.

    Only rows in the opposite state are touched, so `read_at` keeps its original
    value on an already-read row and the returned count reflects real changes.

    Returns:
        The number of notifications whose read state changed.
    """
    column = col(Notification.read_at)
    statement = (
        Notification.live_update()
        .where(col(Notification.user_id) == user_id)
        .where(column.is_(None) if read else column.is_not(None))
    )
    if ids:
        statement = statement.where(col(Notification.id).in_(ids))
    statement = statement.values(read_at=func.now() if read else None)
    result = await session.execute(statement)
    return result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result


async def create_notification(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    description: str | None = None,
    object_type: NotificationObjectType | None = None,
    object_id: UUID | None = None,
) -> Notification:
    """Create an unread notification for ``user_id`` — the internal delivery API.

    Flushes but does not commit: the notification lands atomically with the
    caller's transaction (a rolled-back domain event emits no notification).
    """
    notification = Notification(
        user_id=user_id,
        name=name,
        description=description,
        object_type=object_type,
        object_id=object_id,
    )
    session.add(notification)
    await session.flush()
    await session.refresh(notification, attribute_names=["created_at", "updated_at"])
    return notification
