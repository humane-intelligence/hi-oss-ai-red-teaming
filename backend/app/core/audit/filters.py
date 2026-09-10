"""Query-time filters and sort schema for `GET /v1/audit-logs`."""

from datetime import datetime
from typing import Annotated
from typing import Literal
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel

AuditLogOrderBy = Literal["created_at", "-created_at"]


class AuditLogFilters(BaseModel):
    """Optional filters for `GET /v1/audit-logs`; every field defaults to no constraint."""

    actor_id: Annotated[UUID | None, Query(description="Restrict to one actor (user id).")] = None
    action: Annotated[
        str | None, Query(description="Restrict to one action, e.g. `evaluation_group.publish`.", max_length=64)
    ] = None
    object_type: Annotated[str | None, Query(description="Restrict to one target type.", max_length=64)] = None
    object_id: Annotated[UUID | None, Query(description="Restrict to one target id.")] = None
    created_from: Annotated[datetime | None, Query(description="Only events at/after this UTC timestamp.")] = None
    created_to: Annotated[datetime | None, Query(description="Only events at/before this UTC timestamp.")] = None
