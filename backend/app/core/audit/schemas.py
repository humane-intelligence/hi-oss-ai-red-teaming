"""Response schema for the audit-log endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

if TYPE_CHECKING:
    from app.core.audit.models import AuditLog

_EXAMPLE_AUDIT_LOG: dict[str, Any] = {
    "id": "e3b0c442-98fc-4c14-9afd-4c8b0f2a1a11",
    "actor_id": "27ca992e-3be5-4c44-bd60-5cf585d9f39d",
    "actor_email": "admin@test.com",
    "action": "evaluation_group.publish",
    "object_type": "evaluation_group",
    "object_id": "0716de48-0479-4987-9527-cd6684a04789",
    "before": {"status": "approved"},
    "after": {"status": "published"},
    "context": {},
    "request_id": "56c144d943f547aeaa1cc1807c3a44f6",
    "created_at": "2026-07-08T11:42:07.512000Z",
}


class AuditLogResponse(BaseModel):
    """Public view of one `AuditLog` row."""

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_AUDIT_LOG})

    id: UUID = Field(description="Server-assigned identifier.")
    actor_id: UUID | None = Field(default=None, description="User who performed the action (null for system).")
    actor_email: str | None = Field(default=None, description="Actor's email at the time of the action.")
    action: str = Field(description="`domain.verb` action code.")
    object_type: str | None = Field(default=None, description="Target entity type, if any.")
    object_id: UUID | None = Field(default=None, description="Target entity id, if any.")
    before: dict[str, Any] | None = Field(default=None, description="Curated pre-state (mutations only).")
    after: dict[str, Any] | None = Field(default=None, description="Curated post-state (mutations only).")
    context: dict[str, Any] = Field(description="Extra non-secret metadata.")
    request_id: str | None = Field(default=None, description="Correlates with the access log's request id.")
    created_at: datetime = Field(description="UTC timestamp of the event (append-only).")

    @classmethod
    def from_model(cls, row: AuditLog) -> AuditLogResponse:
        """Project an `AuditLog` ORM row into the public response shape."""
        return cls(
            id=row.id,
            actor_id=row.actor_id,
            actor_email=row.actor_email,
            action=row.action,
            object_type=row.object_type,
            object_id=row.object_id,
            before=row.before,
            after=row.after,
            context=row.context,
            request_id=row.request_id,
            created_at=row.created_at,
        )
