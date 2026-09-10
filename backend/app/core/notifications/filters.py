"""Query-time filter and sort schema for the notifications list endpoint.

`NotificationFilters` is injected via `Depends()` so each field becomes an
individually documented query parameter; `NotificationOrderBy` is a `Literal`
whitelist of orderable columns (leading `-` selects descending).
"""

from typing import Annotated
from typing import Literal

from fastapi import Query
from pydantic import BaseModel

from app.core.notifications.enums import NotificationObjectType

NotificationOrderBy = Literal[
    "created_at",
    "-created_at",
]


class NotificationFilters(BaseModel):
    """Optional filters for `GET /v1/notifications`.

    The owner scope (only the caller's own notifications) is enforced by the
    service regardless of these filters, so a filter can never widen visibility.
    """

    read: Annotated[
        bool | None,
        Query(description="Restrict to read (`true`) or unread (`false`) notifications."),
    ] = None
    object_type: Annotated[
        NotificationObjectType | None,
        Query(description="Restrict to notifications pointing at one object type."),
    ] = None
