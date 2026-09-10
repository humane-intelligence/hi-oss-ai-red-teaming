"""Evaluation export endpoints — asynchronous jobs only (no synchronous inline-generation path).

Two non-obvious invariants:
- Generating any export is an owner/admin action (in-group `owner` or the
  `evaluation_groups:manage` break-glass), gated under the requester's live identity at create,
  in the worker, and again on download — so a detached job can't widen access.
- An authorised export is a whole-group extract (every red-teamer's rows, not just the
  caller's), so `engagement_report` is a complete deliverable. See `ExportScope`.
"""

from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter
from fastapi import Query
from fastapi import Response
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.audit.service import record_audit
from app.core.auth.schemas import SessionUser
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.evaluations.access import caller_can_manage_groups
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.exceptions import ServiceUnavailableError
from app.core.exports.authority import assert_export_authority
from app.core.exports.base import CsvExport
from app.core.exports.catalog import get_export
from app.core.exports.catalog import list_exports
from app.core.exports.enums import ExportJobStatus
from app.core.exports.enums import parse_stored_format
from app.core.exports.jobs import create_export_job
from app.core.exports.jobs import get_export_job
from app.core.exports.jobs import list_export_jobs
from app.core.exports.jobs import soft_delete_export_job
from app.core.exports.models import ExportJob
from app.core.exports.naming import report_filename
from app.core.exports.naming import resolve_scope_title
from app.core.exports.schemas import CsvExportInfo
from app.core.exports.schemas import ExportJobCreate
from app.core.exports.schemas import ExportJobResponse
from app.core.exports.storage import ExportStorage
from app.core.exports.storage import get_export_storage
from app.core.exports.tasks import run_export_job
from app.core.logging import get_logger
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import TERMS_REFUSED
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(tags=["exports"])

logger = get_logger(__name__)


def _export_job_snapshot(job: ExportJob) -> dict[str, object]:
    """Curated snapshot for the audit before/after — the job's scope + lifecycle, no file/secret data."""
    return {
        "template": job.template,
        "status": job.status.value,
        "evaluation_id": str(job.evaluation_id) if job.evaluation_id else None,
        "evaluation_group_id": str(job.evaluation_group_id) if job.evaluation_group_id else None,
    }


def _resolve_template(template: str) -> CsvExport[Any]:
    """Unknown template → 404 (a missing resource), not a 422 body-validation error."""
    export = get_export(template)
    if export is None:
        raise NotFoundError(f"Unknown export template '{template}'.")
    return export


async def _authorize_export(payload: ExportJobCreate, caller: SessionUser, db: DbSession) -> None:
    """Gate job creation on in-group owner / admin authority over the target group (see `assert_export_authority`)."""
    await assert_export_authority(
        db,
        caller_id=caller.id,
        evaluation_id=payload.evaluation_id,
        evaluation_group_id=payload.evaluation_group_id,
        can_manage=caller_can_manage_groups(caller),
        missing_message="Only a group owner or an administrator may export its data.",
    )


async def _assert_download_authority(job: ExportJob, caller: SessionUser, db: DbSession) -> None:
    """Re-check the caller still holds owner/admin authority over the job's target group.

    The stored file carries a whole engagement's data and stays downloadable for the TTL
    window; mirror the worker's live re-check (same `assert_export_authority` gate) so authority
    revoked *after* generation also revokes the download (403 / 404), rather than trusting the
    create-time grant for ~24h.
    """
    await assert_export_authority(
        db,
        caller_id=caller.id,
        evaluation_id=job.evaluation_id,
        evaluation_group_id=job.evaluation_group_id,
        can_manage=caller_can_manage_groups(caller),
        missing_message="You no longer have authority to export this group's data.",
    )


async def _resolve_ready_file(job: ExportJob, caller: SessionUser, db: DbSession) -> tuple[ExportStorage, str]:
    """Download guard for a stored file, returning its storage + file_ref.

    409 unless the job is `ready`; 404 if its TTL lapsed (enforced here, not just by the reaper,
    so an expired file never serves even before a sweep); 403 if the caller's owner/admin
    authority decayed since generation (re-checked live, mirroring the worker).
    """
    if job.status != ExportJobStatus.READY or job.file_ref is None:
        raise ConflictError(f"Export job is not ready (status: {job.status.value}).")
    if job.expires_at is not None and job.expires_at < datetime.now(UTC):
        raise NotFoundError("The export file has expired.")
    await _assert_download_authority(job, caller, db)
    return get_export_storage(), job.file_ref


async def _job_filename(session: AsyncSession, job: ExportJob) -> str:
    """Attachment filename — human title + template + request timestamp + format extension."""
    title = await resolve_scope_title(
        session, evaluation_id=job.evaluation_id, evaluation_group_id=job.evaluation_group_id
    )
    extension = parse_stored_format(job.format, job_id=str(job.id)).value
    return report_filename(scope_title=title, template=job.template, extension=extension, created_at=job.created_at)


_JOB_RESPONSES: dict[int | str, dict[str, Any]] = COMMON_ERROR_RESPONSES | {
    status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
    status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
    status.HTTP_404_NOT_FOUND: problem_response("No such export job for this caller."),
}


@router.get(
    "/exports",
    response_model=Page[CsvExportInfo],
    status_code=status.HTTP_200_OK,
    summary="List export templates",
    description="Return the available evaluation export templates — the pick-list for the exporter.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
    },
)
async def list_exports_endpoint(_caller: CurrentUserDep, pagination: PaginationDep) -> Page[CsvExportInfo]:
    """List the evaluation export templates.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    """
    catalog = list_exports()
    window = catalog[pagination.offset : pagination.offset + pagination.limit]
    items = [CsvExportInfo.from_export(export) for export in window]
    return Page[CsvExportInfo](items=items, total=len(catalog), limit=pagination.limit, offset=pagination.offset)


@router.post(
    "/exports/jobs",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an export",
    description=(
        "Queue a background export job. `template` selects what to export; exactly one of "
        "`evaluation_id` / `evaluation_group_id` sets the scope. Only a group owner or an "
        "administrator may export. Poll `GET /api/v1/exports/jobs/{id}` until `ready`, then download it. "
        "Pass an `idempotency_key` to make a retry safe — a repeat with the same key returns the "
        "already-queued job instead of generating a duplicate. Reusing a key for a *different* "
        "template/scope, or losing a create race on the key, is a `409`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_202_ACCEPTED: {"description": "Job queued."},
        status.HTTP_400_BAD_REQUEST: problem_response("A filter dimension the chosen template doesn't support."),
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller is not the group's owner / an administrator."),
        status.HTTP_404_NOT_FOUND: problem_response("Unknown template, or the target is not found / not visible."),
        status.HTTP_409_CONFLICT: problem_response(
            "`idempotency_key` was already used for a different request, or a concurrent create race was lost."
        ),
        status.HTTP_503_SERVICE_UNAVAILABLE: problem_response("The task broker is unavailable; retry the request."),
    },
)
async def create_export_job_endpoint(
    payload: ExportJobCreate, caller: CurrentUserDep, db: DbSession
) -> ExportJobResponse:
    """Queue a background export job and return its initial `pending` state.

    ### Errors

    * **400 Bad Request** — a filter dimension the chosen `template` doesn't support.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller is not the target group's owner and lacks the manage break-glass.
    * **404 Not Found** — unknown `template`, or the target isn't visible to the caller.
    * **409 Conflict** — the `idempotency_key` was already used for a different template/scope, or a
      concurrent create race on the key was lost.
    * **422 Unprocessable Entity** — neither or both scope targets set.
    * **503 Service Unavailable** — the task broker could not accept the job; the row is rolled back, retry.
    """
    export = _resolve_template(payload.template)
    # Reject a filter dimension the chosen template doesn't honor (400) instead of accepting and
    # silently dropping it — symmetric with the request-time `status` validation, and keeps the
    # stored/audited/fingerprinted filter set honest.
    unsupported = payload.filters.active_dimensions() - export.supported_filters
    if unsupported:
        raise BadRequestError(
            f"Filter(s) {', '.join(sorted(unsupported))} not supported by the '{payload.template}' export."
        )
    # Authorize now (404 not visible / 403 not owner) so a bad request fails at request time.
    # The worker re-runs the same authority resolution from `requested_by_id`, so it can't widen.
    await _authorize_export(payload, caller, db)

    job, created = await create_export_job(
        db,
        template=payload.template,
        export_format=payload.format,
        # Store only the set (non-None) filters as a JSON-safe dict, or null when unfiltered — the
        # detached worker replays it, and it's part of the idempotency fingerprint.
        filters=payload.filters.model_dump(mode="json", exclude_none=True) or None,
        requested_by_id=caller.id,
        evaluation_id=payload.evaluation_id,
        evaluation_group_id=payload.evaluation_group_id,
        idempotency_key=payload.idempotency_key,
    )
    audit_row: AuditLog | None = None
    if created:
        # Not @transactional — record before the manual commit so the audit row commits with the
        # job. Only for a freshly created job; an idempotent replay is not a new creation event.
        audit_row = await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.EXPORT_CREATE,
            object_type="export_job",
            object_id=job.id,
            after=_export_job_snapshot(job),
        )
    await db.commit()
    # Enqueue only after the commit (so the worker can't pick the task up before its
    # row is visible) and only for a freshly created job — an idempotent replay is
    # already queued, so re-enqueueing would duplicate the generation we just deduped.
    if created:
        try:
            run_export_job.delay(str(job.id))
        except Exception as exc:
            # The row is already committed, so drop the orphan rather than leave a phantom
            # `pending` job that never runs (and free its idempotency key for a clean retry).
            # A broker outage is the expected trigger → retryable 503; log the cause so a
            # deterministic enqueue failure (e.g. a mis-registered task) still surfaces to
            # operators instead of hiding entirely behind the 503.
            logger.exception("exports.enqueue_failed", job_id=str(job.id))
            await db.delete(job)
            if audit_row is not None:
                # The audit row committed with the job but has no FK cascade (object_id is a bare
                # UUID), so drop it alongside the job — else it dangles as an export.create pointing
                # at a hard-deleted job.
                await db.delete(audit_row)
            await db.commit()
            raise ServiceUnavailableError("Could not queue the export job; please retry.") from exc
    return ExportJobResponse.from_job(job)


@router.get(
    "/exports/jobs",
    response_model=Page[ExportJobResponse],
    status_code=status.HTTP_200_OK,
    summary="List the caller's export jobs for a scope",
    description=(
        "Return the caller's own export jobs for one target — exactly one of `evaluation_id` / "
        "`evaluation_group_id` — newest first. Backs the downloads list on the evaluation / group "
        "detail views. Owner-scoped like the single-job GET: only jobs the caller requested (and "
        "can therefore download) are listed."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("Neither or both scope targets set."),
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
    },
)
async def list_export_jobs_endpoint(
    caller: CurrentUserDep,
    db: DbSession,
    pagination: PaginationDep,
    evaluation_id: Annotated[UUID | None, Query(description="Scope: a single evaluation.")] = None,
    evaluation_group_id: Annotated[UUID | None, Query(description="Scope: a whole evaluation group.")] = None,
) -> Page[ExportJobResponse]:
    """List the caller's export jobs for one scope.

    ### Errors

    * **400 Bad Request** — neither or both scope targets set. This is the same "exactly one scope"
      invariant the create body enforces, but here it validates *query params*, so it surfaces as a
      400 rather than the create endpoint's Pydantic-body 422 — deliberate, per-layer convention.
    * **401 Unauthorized** — bearer token missing/invalid.
    """
    # Query-param validation → 400 (create validates the same rule in the request body → Pydantic 422).
    if (evaluation_id is None) == (evaluation_group_id is None):
        raise BadRequestError("Exactly one of evaluation_id / evaluation_group_id must be set.")
    jobs, total = await list_export_jobs(
        db,
        requested_by_id=caller.id,
        evaluation_id=evaluation_id,
        evaluation_group_id=evaluation_group_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[ExportJobResponse](
        items=[ExportJobResponse.from_job(job) for job in jobs],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/exports/jobs/{job_id}",
    response_model=ExportJobResponse,
    status_code=status.HTTP_200_OK,
    summary="Get an export job's status",
    description="Return the state of a background export job the caller requested.",
    responses=_JOB_RESPONSES,
)
async def get_export_job_endpoint(job_id: UUID, caller: CurrentUserDep, db: DbSession) -> ExportJobResponse:
    """Return an export job's current state.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — no such job requested by this caller.
    """
    job = await get_export_job(db, job_id, requested_by_id=caller.id)
    return ExportJobResponse.from_job(job)


@router.delete(
    "/exports/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an export job",
    description=(
        "Delete a *finished* export job: removes the stored file and soft-deletes the row, so it stops "
        "listing and can no longer be downloaded. The manual counterpart to the TTL reaper — revoke an "
        "export generated by mistake, or after sharing, before its window lapses. The requester deletes "
        "their own jobs; an `evaluation_groups:manage` admin may also delete any requester's export "
        "(housekeeping after a departed red-teamer). Only `ready`/`failed` jobs are deletable (a `409` "
        "otherwise); a still-generating job would race the worker's finalize and orphan its file."
    ),
    responses=_JOB_RESPONSES
    | {
        status.HTTP_409_CONFLICT: problem_response("The job is still generating; wait for it to finish."),
    },
)
async def delete_export_job_endpoint(job_id: UUID, caller: CurrentUserDep, db: DbSession) -> Response:
    """Delete a *finished* export job (its stored file and row).

    Owner-scoped, with an `evaluation_groups:manage` break-glass that lifts the scope so an admin can
    clean up any requester's export (e.g. after a red-teamer leaves). Unlike download — which *serves*
    a whole group's data and stays strictly owner-scoped — delete is a housekeeping action, so the
    break-glass reaches others' rows without granting read access. No per-group re-check on the owner
    path, so a requester can always clear their own export, even one whose group was since deleted.
    The file removal is best-effort; the row is soft-deleted regardless.

    Restricted to terminal (`ready`/`failed`) jobs: deleting a still-`pending`/`running` job would
    race the worker's finalize write (which could commit `file_ref` after this read), soft-deleting
    a row whose file the reaper — scanning live rows only — could then never reclaim.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — no such export job (for this caller, unless they hold the manage break-glass).
    * **409 Conflict** — the job is still `pending`/`running`; wait for it to finish (or fail).
    """
    job = await get_export_job(db, job_id, requested_by_id=caller.id, can_manage=caller_can_manage_groups(caller))
    if job.status not in (ExportJobStatus.READY, ExportJobStatus.FAILED):
        raise ConflictError(f"Export job is still {job.status.value}; wait for it to finish before deleting.")
    before = _export_job_snapshot(job)
    await soft_delete_export_job(db, job, get_export_storage(), by_id=caller.id)
    # Not @transactional — record before the manual commit so the audit row commits with the delete.
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.EXPORT_DELETE,
        object_type="export_job",
        object_id=job.id,
        before=before,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/exports/jobs/{job_id}/download",
    status_code=status.HTTP_200_OK,
    summary="Download a finished export",
    description="Stream the stored file (CSV or JSON) of a `ready` export job the caller requested.",
    response_class=StreamingResponse,
    responses=_JOB_RESPONSES
    | {
        status.HTTP_200_OK: {
            "content": {"text/csv": {"schema": {"type": "string"}}, "application/json": {"schema": {"type": "string"}}},
            "description": "The export file (CSV or JSON, per the job's format).",
        },
        status.HTTP_403_FORBIDDEN: problem_response("Caller no longer has authority to export this group's data."),
        status.HTTP_404_NOT_FOUND: problem_response(
            "No such export job for this caller, or its file has expired / been removed."
        ),
        status.HTTP_409_CONFLICT: problem_response(
            "The job is not ready for download (still generating, or it failed)."
        ),
    },
)
async def download_export_job_endpoint(job_id: UUID, caller: CurrentUserDep, db: DbSession) -> StreamingResponse:
    """Stream a finished export job's stored file (CSV or JSON, per the job's format).

    The job is owner-scoped (only its requester can download it) and the caller's owner/admin
    authority over the target group is re-checked live on every download — so authority
    revoked after generation revokes the download too, not just at the worker.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller no longer holds owner/admin authority over the target group.
    * **404 Not Found** — no such job for this caller, or its file has expired / been removed.
    * **409 Conflict** — the job is still `pending`/`running`, or it `failed`.
    """
    job = await get_export_job(db, job_id, requested_by_id=caller.id)
    storage, file_ref = await _resolve_ready_file(job, caller, db)
    # Open EAGERLY (offloaded — local open / S3 get_object may block): a vanished file raises
    # FileNotFoundError here, before the 200 + headers commit, so it 404s cleanly instead of a
    # truncated body.
    try:
        stream = await to_thread.run_sync(lambda: storage.open(file_ref))
    except FileNotFoundError:
        raise NotFoundError("The export file has expired or been removed.") from None
    filename = await _job_filename(db, job)
    # Release the pooled connection before the (possibly long/slow) stream — get_db would
    # otherwise pin it for the whole response, exhausting the pool under concurrent downloads.
    # Everything from the DB (job row + authority) is already resolved above; the stream never
    # touches it. Mirrors the chat/stream endpoint.
    await db.close()
    return StreamingResponse(
        stream,
        media_type=parse_stored_format(job.format, job_id=str(job.id)).media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
