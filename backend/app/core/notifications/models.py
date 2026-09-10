"""Notifications table — a per-user in-app message with an optional read mark.

A `Notification` is delivered to one `user_id`; `read_at` NULL means unread.
`object_type` / `object_id` optionally point the notification at a domain row
(e.g. an evaluation) so the frontend can deep-link — a loose reference, not a
cross-table FK: the target may be soft-deleted or gone. Rows are created by
`create_notification` (internal), plus a direct insert from the email worker, which
runs on a sync session and cannot call it (`app/core/email/tasks.py`); there is no
HTTP create path.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Text
from sqlmodel import Field

from app.core.base_model import BaseModel


class Notification(BaseModel, table=True):
    __tablename__ = "notifications"
    __table_args__ = (
        # Serves the owner-scoped, newest-first list (the `read` filter is a
        # residual predicate — read_at is not part of the index).
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
    )

    # No standalone index: the composite (user_id, created_at) above covers
    # user-scoped lookups too.
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False)
    name: str = Field(max_length=255, nullable=False)
    description: str | None = Field(default=None, sa_type=Text, sa_column_kwargs={"nullable": True})
    read_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Loose reference to a domain row; a plain string (see NotificationObjectType),
    # validated at the edge. No FK — the target may be soft-deleted or absent.
    object_type: str | None = Field(default=None, max_length=64)
    object_id: uuid.UUID | None = Field(default=None)
