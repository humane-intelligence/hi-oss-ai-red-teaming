"""Export-domain tables — `ExportJob`, one asynchronous export (CSV or JSON) generation request.

An `ExportJob` is the durable record of a background export: which `template` to
run and over which scope (exactly one of `evaluation_id` / `evaluation_group_id`),
who asked for it (`requested_by_id`), and where the finished file landed
(`file_ref`) once `status` reaches `ready`. The scope is a *pointer* the worker
re-resolves under the requester's live visibility at run time — it is never
trusted to widen access, so the target ids are plain nullable FKs (SET NULL on a
hard delete) rather than a carrier of authority.

`expires_at` is stamped when the job finishes so the reaper can drop the stored
file (and the row) once it lapses; it is null while the job is still pending or
running.

`idempotency_key` collapses a burst of identical create requests (an impatient
double-click) onto one job: a repeat from the same requester replays the stored
job instead of queuing another generation. Partial-unique per requester while
live — a soft-deleted (reaped) job frees its key.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.base_model import BaseModel
from app.core.exports.enums import ExportJobStatus


class ExportJob(BaseModel, table=True):
    __tablename__ = "export_jobs"
    __table_args__ = (
        # Idempotent create: a repeated key from the same requester replays the job
        # rather than duplicating it. Partial — scoped to live, keyed rows, so a
        # reaped job frees its key and distinct requesters never collide.
        Index(
            "ix_export_jobs_requester_idempotency_key",
            "requested_by_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    template: str = Field(nullable=False)
    # Output format the worker renders to (`csv`/`json`). Bare str (like `template`), validated at
    # the edge against `ExportFormat` — so a new format is code-only, no Postgres-enum migration.
    format: str = Field(default="csv", nullable=False, sa_column_kwargs={"server_default": text("'csv'")})
    # Exactly one scope target is set; the worker validates the presence-of-one at run.
    # SET NULL (not CASCADE) so a hard-deleted target leaves the finished file/audit intact.
    evaluation_id: uuid.UUID | None = Field(default=None, foreign_key="evaluations.id", ondelete="SET NULL")
    evaluation_group_id: uuid.UUID | None = Field(default=None, foreign_key="evaluation_groups.id", ondelete="SET NULL")
    requested_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True, ondelete="CASCADE")
    status: ExportJobStatus = Field(
        default=ExportJobStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            ExportJobStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="exportjobstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True, "server_default": text("'pending'")},
    )
    # Opaque storage handle set by the worker on success; interpreted only by the storage backend.
    file_ref: str | None = Field(default=None)
    # Human-readable failure reason when `status == failed` (never upstream provider internals).
    error: str | None = Field(default=None, sa_type=Text)
    expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Client-supplied key that dedups a burst of identical creates (see module docstring).
    idempotency_key: uuid.UUID | None = Field(default=None)
    # Row filters chosen at create (see `ExportFilters`), replayed by the detached worker; part of
    # the idempotency fingerprint. Stored as the JSON-safe dict of set (non-None) filters, or null.
    filters: dict[str, Any] | None = Field(default=None, sa_type=JSONB, sa_column_kwargs={"nullable": True})
