"""Audit-log table — one append-only row per sensitive action or access.

Modeled on `OutboundEmail` (FK-less, JSONB, `BaseModel`) but **append-only**: rows
are never updated or soft-deleted, so `created_at` is the event time and the
inherited `deleted_at` stays NULL (accepted — we reuse `id`/`created_at`).

FK-less on purpose: the log outlives the actor/target it references (a deleted user
or evaluation must not cascade-drop its audit trail). `object_type`/`object_id` are
the polymorphic target (same shape as `ObjectRoleAssignment`), nullable for actions
with no target (e.g. a failed login). `before`/`after` hold a **curated** snapshot of
non-secret fields for mutations, NULL for access events.
"""

import uuid
from typing import Any

from sqlalchemy import Index
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.base_model import BaseModel


class AuditLog(BaseModel, table=True):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_object", "object_type", "object_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    # Actor — the human. NULL for system / unauthenticated actions (e.g. failed login).
    # No FK: the trail survives the user's deletion. `actor_email` denormalised for
    # readability without a join.
    actor_id: uuid.UUID | None = Field(default=None, index=True)
    actor_email: str | None = Field(default=None, max_length=320)

    # `domain.verb` from the `AuditAction` catalog (plain string — catalog grows without a migration).
    action: str = Field(index=True, max_length=64)

    # Polymorphic target (no FK), nullable for target-less actions.
    object_type: str | None = Field(default=None, max_length=64)
    object_id: uuid.UUID | None = Field(default=None)

    # Curated, non-secret snapshots for mutations; NULL for access/read events.
    before: dict[str, Any] | None = Field(default=None, sa_type=JSONB, sa_column_kwargs={"nullable": True})
    after: dict[str, Any] | None = Field(default=None, sa_type=JSONB, sa_column_kwargs={"nullable": True})

    # Extra non-secret metadata (e.g. {"template": ...}, {"email": ...}).
    context: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )

    # Correlates with the `http_request` access log's request id (from structlog contextvars).
    request_id: str | None = Field(default=None, index=True, max_length=64)
