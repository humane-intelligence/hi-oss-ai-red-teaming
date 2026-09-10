"""Closed-set enums for asynchronous export jobs, surfaced on the wire.

`ExportJobStatus` is persisted as a Postgres enum column; `ExportFormat` is deliberately a bare
`str` column (see its docstring) — so the two differ in how a new member is added (migration vs
code-only). Both are edge-validated against these enums.
"""

from enum import StrEnum

from app.core.logging import get_logger

logger = get_logger(__name__)


class ExportFormat(StrEnum):
    """Output format a job renders to. Chosen at create (the worker renders those bytes once).

    Persisted as a plain `str` column (like `template`), not a Postgres enum — so adding a
    format is a code change, no `ALTER TYPE` migration. `csv` is the default for back-compat.
    """

    CSV = "csv"
    JSON = "json"

    @property
    def media_type(self) -> str:
        """HTTP `Content-Type` for this format's rendered bytes (download header + stored-object metadata)."""
        return "application/json" if self is ExportFormat.JSON else "text/csv"


def parse_stored_format(value: str, *, job_id: str | None = None) -> ExportFormat:
    """Coerce a stored bare-`str` `format` to `ExportFormat`, falling back to `CSV` on an unknown value.

    The column has no DB CHECK (code-only validation), so a script/backfill could write a value
    outside the enum. Read/render paths must not 500 on it: fall back to the default and log a
    warning so the bad row surfaces. Normal writes are Pydantic-validated, so this only guards the
    corrupt-data path.

    Args:
        value: The stored `format` string to coerce.
        job_id: The owning job's id, threaded onto the warning so several corrupt rows in one
            list response stay distinguishable.
    """
    try:
        return ExportFormat(value)
    except ValueError:
        logger.warning("exports.unknown_stored_format", stored_format=value, job_id=job_id)
        return ExportFormat.CSV


class ExportJobStatus(StrEnum):
    """Lifecycle of a background export job, orthogonal to soft-delete (`deleted_at`).

    A job lands `pending`, a worker flips it to `running`, and it finalises to
    `ready` (file written, downloadable) or `failed` (generation raised — `error`
    carries the reason). The set is closed — adding a value needs a migration
    (`ALTER TYPE ... ADD VALUE`).
    """

    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
