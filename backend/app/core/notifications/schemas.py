"""Request/response schemas for the notifications endpoints.

`NotificationResponse` is the public projection (`read` is derived from
`read_at`). `MarkNotificationsRequest` backs the bulk mark endpoint — an empty
`ids` list means *all* of the caller's notifications. `NotificationMarkResult`
reports how many rows actually flipped state.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.notifications.enums import NotificationObjectType

if TYPE_CHECKING:
    from app.core.notifications.models import Notification


class NotificationResponse(BaseModel):
    """Public view of a `Notification` row.

    Build via `NotificationResponse.from_model(notification)` so the projection
    stays in one place.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                "name": "Evaluation approved",
                "description": "Your evaluation 'Prompt-injection sweep' was approved.",
                "read": False,
                "read_at": None,
                "object_type": "evaluation",
                "object_id": "a1b2c3d4-1111-2222-3333-444455556666",
                "user_id": "b2c3d4e5-2222-3333-4444-555566667777",
                "created_at": "2026-01-01T12:00:00Z",
                "updated_at": "2026-01-01T12:00:00Z",
            }
        }
    )

    id: UUID = Field(description="Server-assigned identifier.")
    name: str = Field(description="Short title of the notification.")
    description: str | None = Field(description="Longer body text, if any.")
    read: bool = Field(description="Whether the recipient has read it (derived from `read_at`).")
    read_at: datetime | None = Field(description="UTC timestamp when it was marked read; `null` while unread.")
    object_type: NotificationObjectType | None = Field(description="Domain object the notification points at, if any.")
    object_id: UUID | None = Field(description="Id of that object, if any.")
    user_id: UUID = Field(description="Recipient of the notification.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")

    @classmethod
    def from_model(cls, notification: Notification) -> NotificationResponse:
        """Project a `Notification` ORM row into the public response shape."""
        return cls(
            id=notification.id,
            name=notification.name,
            description=notification.description,
            read=notification.read_at is not None,
            read_at=notification.read_at,
            object_type=NotificationObjectType(notification.object_type) if notification.object_type else None,
            object_id=notification.object_id,
            user_id=notification.user_id,
            created_at=notification.created_at,
            updated_at=notification.updated_at,
        )


class MarkNotificationsRequest(BaseModel):
    """Payload accepted by `POST /v1/notifications/mark`.

    `read` sets the target state (read/unread); `ids` selects which of the
    caller's notifications to flip. An empty `ids` means **all** of them.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"ids": [], "read": True}})

    ids: list[UUID] = Field(
        default_factory=list, description="Notification ids to mark; empty means all of the caller's."
    )
    read: bool = Field(description="Target state: `true` marks read, `false` marks unread.")


class NotificationMarkResult(BaseModel):
    """Result of a bulk mark: how many rows actually changed state."""

    updated: int = Field(description="Number of notifications whose read state changed.", examples=[3])
