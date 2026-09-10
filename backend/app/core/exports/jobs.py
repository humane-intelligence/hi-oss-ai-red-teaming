"""Service layer for background export jobs — create + owner-scoped read/list.

Jobs are owner-scoped: a caller sees, lists, and downloads only the jobs they
requested (`requested_by_id`). Owner/admin authority (`assert_export_authority`)
and target visibility are enforced at create (in the endpoint) and re-resolved by
the worker; this layer is just the row store.
"""

from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.exports.models import ExportJob
from app.core.exports.storage import STORAGE_ERRORS
from app.core.exports.storage import ExportStorage
from app.core.logging import get_logger
from app.core.pagination import paginate

logger = get_logger(__name__)


async def create_export_job(
    session: AsyncSession,
    *,
    template: str,
    requested_by_id: UUID,
    export_format: str,
    filters: dict[str, Any] | None = None,
    evaluation_id: UUID | None = None,
    evaluation_group_id: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> tuple[ExportJob, bool]:
    """Persist a `pending` job, or replay an existing one for a repeated `idempotency_key`.

    Returns:
        (job, created). `created` is False when an `idempotency_key` matched a job the
        caller already has queued — the caller must **not** re-enqueue it. Only the
        commit + `run_export_job` enqueue happen at the endpoint.
    """
    if idempotency_key is not None:
        existing = await _find_by_idempotency_key(session, requested_by_id, idempotency_key)
        if existing is not None:
            _assert_same_request(
                existing, template, export_format, filters, evaluation_id, evaluation_group_id, idempotency_key
            )
            return existing, False

    job = ExportJob(
        template=template,
        format=export_format,
        filters=filters,
        requested_by_id=requested_by_id,
        evaluation_id=evaluation_id,
        evaluation_group_id=evaluation_group_id,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the create race to a concurrent request with the same key — the
        # partial-unique index rejected this row. Return the winner's job instead.
        await session.rollback()
        if idempotency_key is not None:
            winner = await _find_by_idempotency_key(session, requested_by_id, idempotency_key)
            if winner is not None:
                _assert_same_request(
                    winner, template, export_format, filters, evaluation_id, evaluation_group_id, idempotency_key
                )
                return winner, False
        raise ConflictError("Concurrent export create; retry the request.") from None
    await session.refresh(job)
    return job, True


def _assert_same_request(
    job: ExportJob,
    template: str,
    export_format: str,
    filters: dict[str, Any] | None,
    evaluation_id: UUID | None,
    evaluation_group_id: UUID | None,
    idempotency_key: UUID,
) -> None:
    """Reject a reused `idempotency_key` that carries a *different* request.

    Standard idempotency-key semantics (Stripe et al.): a key fingerprints one request, so
    replaying it with a different template/format/filters/scope must be a client error, not a
    silent replay of the wrong resource's job. A same-body replay passes and returns the stored job.
    """
    if (job.template, job.format, job.filters, job.evaluation_id, job.evaluation_group_id) != (
        template,
        export_format,
        filters,
        evaluation_id,
        evaluation_group_id,
    ):
        raise ConflictError(f"idempotency_key {idempotency_key} was already used for a different export request.")


async def _find_by_idempotency_key(
    session: AsyncSession, requested_by_id: UUID, idempotency_key: UUID
) -> ExportJob | None:
    result = await session.execute(
        ExportJob.live_select().where(
            col(ExportJob.requested_by_id) == requested_by_id,
            col(ExportJob.idempotency_key) == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def list_export_jobs(
    session: AsyncSession,
    *,
    requested_by_id: UUID,
    evaluation_id: UUID | None,
    evaluation_group_id: UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[ExportJob], int]:
    """List the caller's own live jobs for one scope, newest first.

    Owner-scoped like `get_export_job` (a caller only lists jobs they requested) and
    TTL-expired jobs are excluded. Authority is NOT re-checked per row here — it is
    re-verified live at download — so a job whose target was soft-deleted or
    whose owner role was revoked mid-TTL can still list but 403/404s on fetch. Exactly
    one of `evaluation_id` / `evaluation_group_id` is set (enforced by the route).
    """
    now = datetime.now(UTC)
    statement = (
        ExportJob.live_select()
        .where(col(ExportJob.requested_by_id) == requested_by_id)
        .where(or_(col(ExportJob.expires_at).is_(None), col(ExportJob.expires_at) >= now))
    )
    if evaluation_id is not None:
        statement = statement.where(col(ExportJob.evaluation_id) == evaluation_id)
    if evaluation_group_id is not None:
        statement = statement.where(col(ExportJob.evaluation_group_id) == evaluation_group_id)
    statement = statement.order_by(col(ExportJob.created_at).desc())
    return await paginate(session, statement, limit=limit, offset=offset)


async def get_export_job(
    session: AsyncSession, job_id: UUID, *, requested_by_id: UUID, can_manage: bool = False
) -> ExportJob:
    """Fetch one live job by id.

    Owner-scoped by default (`requested_by_id` — no existence leak across requesters), else 404.
    `can_manage` (the `evaluation_groups:manage` break-glass) lifts that scope so an admin can reach
    any requester's job — used by delete so a manager can clean up an export a departed red-teamer left.
    """
    statement = ExportJob.live_select().where(col(ExportJob.id) == job_id)
    if not can_manage:
        statement = statement.where(col(ExportJob.requested_by_id) == requested_by_id)
    job = (await session.execute(statement)).scalar_one_or_none()
    if job is None:
        raise NotFoundError(f"Export job {job_id} not found.")
    return job


async def soft_delete_export_job(
    session: AsyncSession, job: ExportJob, storage: ExportStorage, *, by_id: UUID | None
) -> None:
    """Remove an export's stored file (best-effort) and soft-delete its row.

    Shared by the manual delete endpoint and the TTL reaper so both behave identically: a storage
    failure (missing/locked file, backend error) is logged and swallowed — the row is still
    soft-deleted, so a lingering file can never keep the job "live".
    """
    if job.file_ref is not None:
        try:
            await storage.delete(job.file_ref)
            job.file_ref = None  # drop the handle only once the file is actually gone (keep it on a swallowed failure)
        except STORAGE_ERRORS as exc:
            logger.warning("exports.delete_file_failed", job_id=str(job.id), error=str(exc))
    job.soft_delete(by_id)
    session.add(job)
