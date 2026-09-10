"""Request / response schemas for the CSV export endpoints."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from app.core.exports.base import CsvExport
from app.core.exports.enums import ExportFormat
from app.core.exports.enums import ExportJobStatus
from app.core.exports.enums import parse_stored_format
from app.core.exports.filters import ExportFilters
from app.core.exports.models import ExportJob


class CsvExportInfo(BaseModel):
    """One export template shown in the picker (`GET /api/v1/exports`)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key": "flags",
                "name": "Message flags",
                "description": "One row per message flag in the evaluation.",
                "permission": "flags:read",
            }
        }
    )

    key: str = Field(description="Template key — pass as `template` in the `POST /api/v1/exports/jobs` body.")
    name: str = Field(description="Human-readable template name.")
    description: str = Field(description="What the export contains.")
    permission: str = Field(
        description=(
            "The data-read permission this export's rows correspond to (informational). Running an "
            "export is gated on in-group owner / `evaluation_groups:manage` authority, not this."
        )
    )

    @classmethod
    def from_export(cls, export: CsvExport) -> Self:
        return cls(key=export.key, name=export.name, description=export.description, permission=export.permission)


class ExportJobCreate(BaseModel):
    """Request to generate an export in the background. Exactly one scope target is set."""

    template: str = Field(description="Template key — see `GET /api/v1/exports`.")
    format: ExportFormat = Field(default=ExportFormat.CSV, description="Output format (`csv` or `json`).")
    filters: ExportFilters = Field(
        default_factory=ExportFilters, description="Optional row filters (see `ExportFilters`)."
    )
    evaluation_id: UUID | None = Field(default=None, description="Export a single evaluation.")
    evaluation_group_id: UUID | None = Field(default=None, description="Export a whole evaluation group.")
    idempotency_key: UUID | None = Field(
        default=None,
        description=(
            "Optional client key that dedups a burst of identical creates (e.g. an impatient "
            "double-click): a repeat from the same caller returns the already-queued job instead "
            "of starting another. Send a fresh key per deliberate export."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> Self:
        if (self.evaluation_id is None) == (self.evaluation_group_id is None):
            msg = "Exactly one of evaluation_id / evaluation_group_id must be set."
            raise ValueError(msg)
        return self


class ExportJobResponse(BaseModel):
    """State of a background export job — polled until `status` reaches `ready`/`failed`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-1111-2222-3333-444455556666",
                "template": "engagement_report",
                "format": "csv",
                "status": "ready",
                "evaluation_id": None,
                "evaluation_group_id": "b2c3d4e5-2222-3333-4444-555566667777",
                "error": None,
                "created_at": "2026-07-01T12:00:00Z",
                "expires_at": "2026-07-02T12:00:00Z",
            }
        }
    )

    id: UUID
    template: str
    format: ExportFormat
    status: ExportJobStatus
    evaluation_id: UUID | None
    evaluation_group_id: UUID | None
    error: str | None = Field(description="Failure reason when `status` is `failed`, else null.")
    created_at: datetime
    expires_at: datetime | None = Field(
        description=(
            "TTL deadline, set once the job reaches `ready` or `failed` (the reaper sweeps it after); "
            "null while `pending`/`running`."
        )
    )

    @classmethod
    def from_job(cls, job: ExportJob) -> Self:
        return cls(
            id=job.id,
            template=job.template,
            format=parse_stored_format(job.format, job_id=str(job.id)),
            status=job.status,
            evaluation_id=job.evaluation_id,
            evaluation_group_id=job.evaluation_group_id,
            error=job.error,
            created_at=job.created_at,
            expires_at=job.expires_at,
        )
