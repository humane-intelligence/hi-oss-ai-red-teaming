"""Celery tasks for asynchronous exports.

`run_export_job` generates a stored export file — CSV or JSON per `job.format` — in the
background (every export is async — there is no inline path). It re-resolves the requester's
live visibility + owner/admin authority from `requested_by_id` (the job carries only *pointers*
to the scope, never authority) and reuses the same scoped fetchers, so a detached job can never
widen access.

The task runs its body inside `asyncio.run` because it reuses the async service
layer (visibility resolution + the export fetchers) unchanged; `async_session_scope`
builds a per-run async engine for that (see app/workers/session.py).

`reap_expired_export_jobs` drops finished files (and their rows) once they lapse —
the storage/TTL analogue of the streaming reaper.
"""

import asyncio
import contextlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from botocore.exceptions import ClientError
from celery import shared_task
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.schemas import SessionUser
from app.core.auth.services.roles import effective_permissions
from app.core.auth.services.users import get_user
from app.core.config import get_settings
from app.core.evaluations.access import caller_can_manage_groups
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.exceptions import APIError
from app.core.exports.authority import assert_export_authority
from app.core.exports.catalog import get_export
from app.core.exports.enums import ExportJobStatus
from app.core.exports.enums import parse_stored_format
from app.core.exports.filters import ExportFilters
from app.core.exports.generation import resolve_group_evaluations
from app.core.exports.generation import stream_export
from app.core.exports.jobs import soft_delete_export_job
from app.core.exports.models import ExportJob
from app.core.exports.notifications import emit_export_completion
from app.core.exports.storage import STORAGE_ERRORS
from app.core.exports.storage import get_export_storage
from app.core.logging import get_logger
from app.workers.session import async_session_scope

logger = get_logger(__name__)


async def _announce(  # noqa: PLR0913 — mirrors emit_export_completion's fields (a carrier object would just move the surface area)
    session: AsyncSession,
    *,
    job_id: UUID,
    requested_by_id: UUID,
    evaluation_id: UUID | None,
    evaluation_group_id: UUID | None,
    template: str,
    export_format: str,
    created_at: datetime,
    succeeded: bool,
    error: str | None = None,
) -> None:
    """Best-effort completion announcement in the caller's transaction.

    Wraps `emit_export_completion` in a SAVEPOINT: forward-atomicity is kept (a rolled-back
    finalize/terminate discards the savepoint, so nothing is announced), but an announcement
    failure (a DB blip on the audit/notification writes) is rolled back to the savepoint and
    swallowed — it must never undo the export itself or abort the reaper's batch. Mirrors the
    email side, which was already best-effort.
    """
    try:
        async with session.begin_nested():
            await emit_export_completion(
                session,
                job_id=job_id,
                requested_by_id=requested_by_id,
                evaluation_id=evaluation_id,
                evaluation_group_id=evaluation_group_id,
                template=template,
                export_format=export_format,
                created_at=created_at,
                succeeded=succeeded,
                error=error,
            )
    except Exception:  # best-effort: a notify failure must not fail/undo the export
        logger.warning("exports.notify_failed", job_id=str(job_id), exc_info=True)


# Exception classes Celery's `autoretry_for` matches. The retry only actually fires when the
# except block re-raises, which it gates on `_is_transient` — so a permanent ClientError never
# reaches this. Permanent reasons (`_ExportJobError`, `APIError`) fail immediately and are recorded.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (OperationalError, ClientError)

# boto3 collapses every AWS API error into a single `ClientError` — 4xx and 5xx alike. Only
# server faults / throttling are worth retrying; a permanent misconfiguration (AccessDenied,
# NoSuchBucket, InvalidAccessKeyId) must fail fast rather than burn the retry budget. These are
# the throttle codes that carry a 4xx status (5xx is matched by HTTP status directly).
_TRANSIENT_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {"Throttling", "ThrottlingException", "TooManyRequestsException", "SlowDown", "RequestTimeout"}
)
_MIN_SERVER_ERROR_STATUS = 500  # HTTP 5xx and up — server-side, worth retrying


def _is_transient(exc: Exception) -> bool:
    """Whether a failure is worth a retry: a DB blip, or an S3 server fault / throttle.

    Discriminates `ClientError` by HTTP status (5xx) and a throttle-code allowlist so a permanent
    4xx misconfiguration returns False and fails fast — mirroring how `_MISSING_CODES` maps a 404.
    """
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, ClientError):
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code")
        return http_status >= _MIN_SERVER_ERROR_STATUS or code in _TRANSIENT_CLIENT_ERROR_CODES
    return False


class _ExportJobError(Exception):
    """A terminal, requester-facing reason a job can't be produced (marks it `failed`)."""


async def _load_caller(session: AsyncSession, user_id: UUID) -> SessionUser:
    """Reconstruct the requester's identity from the *live* DB (permissions included).

    Mirrors the JWT mint (`effective_permissions` over the user's live roles), so a
    role revoked after the job was queued is honoured — the job never runs with
    stale-elevated access. `provider` is irrelevant to visibility and set to a
    marker.
    """
    user = await get_user(session, user_id)
    return SessionUser(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        first_name=user.first_name,
        last_name=user.last_name,
        provider="system",
        permissions=frozenset(effective_permissions(user)),
    )


async def _resolve_scope(session: AsyncSession, job: ExportJob, caller: SessionUser) -> list[Evaluation]:
    """Re-resolve the job's scope under the requester's live visibility + owner authority (raises if gone).

    Authority is asserted via the one shared `assert_export_authority` gate — the same one the
    create + download endpoints use — so a role revoked after queueing denies the detached job too
    and the three sites can't diverge. The single-evaluation branch then reloads the evaluation row
    it must return (one extra indexed lookup on the background path — the price of one gate).
    """
    can_manage = caller_can_manage_groups(caller)
    await assert_export_authority(
        session,
        caller_id=caller.id,
        evaluation_id=job.evaluation_id,
        evaluation_group_id=job.evaluation_group_id,
        can_manage=can_manage,
        missing_message="Requester is no longer the group's owner.",
    )
    if job.evaluation_id is not None:
        evaluation = await get_evaluation(session, job.evaluation_id, caller_id=caller.id, can_manage=can_manage)
        return [evaluation]
    if job.evaluation_group_id is not None:
        return await resolve_group_evaluations(session, caller, job.evaluation_group_id, can_manage=can_manage)
    raise _ExportJobError("Export job has no scope target.")


@shared_task(
    name="app.core.exports.tasks.run_export_job",
    bind=True,
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_export_job(self, job_id: str) -> dict[str, str]:
    """Generate a stored export file (CSV or JSON, per `job.format`) for an `ExportJob`.

    Idempotent + single-writer: a `ready` job is a no-op on redelivery, and a
    compare-and-swap claims the `pending → running` transition, so a concurrent
    redelivery (visibility-timeout expiry) can't have two workers write the same
    stored file. A worker that crashed mid-run leaves the row `running`; the reaper's
    stuck-job sweep fails it (re-requestable) rather than a second worker re-running it.

    Transient failures (DB blip, S3 5xx/throttle) retry with backoff (`autoretry_for`,
    `max_retries=3`): the row is reset to `pending` so the retry re-claims it. Permanent
    failures — and an exhausted retry budget — are recorded on the row (`status=failed`,
    `error`) for the requester to see and re-request.
    """
    return asyncio.run(_run_export_job(UUID(job_id), retries=self.request.retries, max_retries=self.max_retries))


async def _run_export_job(job_id: UUID, *, retries: int, max_retries: int) -> dict[str, str]:
    async with async_session_scope() as session:
        job = await session.get(ExportJob, job_id)
        if job is None:
            logger.warning("exports.job.missing", job_id=str(job_id))
            return {"status": "missing"}
        if job.status == ExportJobStatus.READY:
            return {"status": ExportJobStatus.READY.value}

        # Claim the job with a compare-and-swap: only a `pending` row transitions to `running`.
        # A second concurrent delivery (the broker's visibility_timeout can expire during a long
        # export → redelivery to a second worker) matches no `pending` row and bails — so two
        # workers never `open("wb")` the same local `{job_id}.csv` and interleave it into a
        # corrupt file (S3 is safe via its own UploadId). A worker that crashed mid-run leaves the
        # row `running`; the reaper's stuck-job sweep fails it (re-requestable), not a re-run here.
        claimed = await session.execute(
            ExportJob.live_update()
            .where(col(ExportJob.id) == job_id, col(ExportJob.status) == ExportJobStatus.PENDING)
            .values(status=ExportJobStatus.RUNNING)
        )
        await session.commit()
        if claimed.rowcount == 0:  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
            await session.refresh(job)
            logger.info("exports.job.skip", job_id=str(job_id), status=job.status.value)
            return {"status": job.status.value}

        try:
            # Resolved first so it's always bound for the failure handler's cleanup key, even if an
            # earlier step raises. `parse_stored_format` never raises (unknown → CSV + warn).
            export_format = parse_stored_format(job.format, job_id=str(job_id))
            caller = await _load_caller(session, job.requested_by_id)
            export = get_export(job.template)
            if export is None:
                raise _ExportJobError(f"Unknown export template '{job.template}'.")
            # Visibility + owner/admin authority re-resolved under the requester's live identity.
            evaluations = await _resolve_scope(session, job, caller)

            storage = get_export_storage()
            # The format's value doubles as the file extension ("csv"/"json"); one deterministic key
            # so redelivery overwrites and the failure/supersede cleanup targets the same file.
            file_key = f"{job.id}.{export_format.value}"
            file_ref = await storage.save(
                file_key,
                stream_export(
                    session,
                    export,
                    caller,
                    evaluations,
                    export_format=export_format,
                    export_filters=ExportFilters.model_validate(job.filters or {}),
                ),
                content_type=export_format.media_type,
            )

            # Finalise with a compare-and-swap on `running` (mirroring the claim): if the row was
            # transitioned away while we generated — e.g. the stuck-job reaper failed it — don't
            # resurrect it. Drop the file we just wrote (nothing dangles) and report the current state.
            finalized = await session.execute(
                ExportJob.live_update()
                .where(col(ExportJob.id) == job_id, col(ExportJob.status) == ExportJobStatus.RUNNING)
                .values(
                    status=ExportJobStatus.READY,
                    file_ref=file_ref,
                    error=None,
                    expires_at=datetime.now(UTC) + timedelta(seconds=get_settings().export_job_ttl_seconds),
                )
            )
            if finalized.rowcount != 0:  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
                # Announce inside the same transaction as the READY flip so a rolled-back
                # finalize emits nothing (only the CAS winner — not a superseded row). Best-effort
                # (`_announce`): a notify blip must not undo the just-generated export.
                await _announce(
                    session,
                    job_id=job_id,
                    requested_by_id=job.requested_by_id,
                    evaluation_id=job.evaluation_id,
                    evaluation_group_id=job.evaluation_group_id,
                    template=job.template,
                    export_format=job.format,
                    created_at=job.created_at,
                    succeeded=True,
                )
            await session.commit()
            if finalized.rowcount == 0:  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
                with contextlib.suppress(*STORAGE_ERRORS):
                    await storage.delete(file_key)
                await session.refresh(job)
                logger.warning("exports.job.superseded", job_id=str(job_id), status=job.status.value)
                return {"status": job.status.value}
            logger.info("exports.job.ready", job_id=str(job_id), evaluations=len(evaluations))
            return {"status": ExportJobStatus.READY.value}
        except Exception as exc:
            return await _handle_run_failure(
                session, job_id, exc, export_format=export_format.value, retries=retries, max_retries=max_retries
            )


async def _handle_run_failure(
    session: AsyncSession, job_id: UUID, exc: Exception, *, export_format: str, retries: int, max_retries: int
) -> dict[str, str]:
    """Roll back a failed generation and either schedule a retry or record the terminal failure.

    Re-raises (so Celery's `autoretry_for` fires) when the failure is transient, budget remains,
    and the row is still `running`; otherwise returns the status dict.
    """
    await session.rollback()
    # A failure mid-stream may have left a partial file at the deterministic key; drop it
    # (best-effort) so a failed/retried job leaks nothing to disk. Suppress only storage errors
    # (incl. a backend ClientError) so cleanup can't mask the original failure.
    with contextlib.suppress(*STORAGE_ERRORS):
        await get_export_storage().delete(f"{job_id}.{export_format}")
    # Transient (DB blip, S3 5xx/throttle) with budget left: reset to `pending` so the retry
    # re-claims it, then re-raise so Celery schedules the retry with backoff. Permanent errors and
    # an exhausted budget fall through to a recorded failure. If the reset commit itself can't land
    # (the transient cause is a still-down DB), the row stays `running` and the retries no-op — the
    # stuck-job reaper is the backstop that eventually fails it for re-request.
    if _is_transient(exc) and retries < max_retries:
        # Reset with a compare-and-swap on `running` (mirroring claim/finalize) so this can't race
        # the stuck-job reaper: if the reaper already flipped the row `running → failed` while we
        # were generating, `rowcount == 0` and we must NOT blindly write it back to `pending` —
        # that would resurrect a terminated job and inherit the reaper's `expires_at`, breaking the
        # "only ready/failed carry a TTL" invariant. A live `running` row is reset (expires_at still NULL).
        requeued = await session.execute(
            ExportJob.live_update()
            .where(col(ExportJob.id) == job_id, col(ExportJob.status) == ExportJobStatus.RUNNING)
            .values(status=ExportJobStatus.PENDING)
        )
        await session.commit()
        if requeued.rowcount != 0:  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
            logger.warning("exports.job.retry", job_id=str(job_id), error=str(exc), retries=retries)
            raise exc  # propagate so Celery's autoretry_for schedules the retry with backoff
        superseded = await session.get(ExportJob, job_id)
        status_value = superseded.status.value if superseded is not None else "missing"
        logger.warning("exports.job.retry_superseded", job_id=str(job_id), status=status_value)
        return {"status": status_value}
    failed = await session.get(ExportJob, job_id)
    if failed is None:
        logger.warning("exports.job.failed", job_id=str(job_id), error=str(exc))
        return {"status": ExportJobStatus.FAILED.value}
    message = _failure_message(exc)
    # CAS on `running` (mirroring claim/finalize): if the stuck-job reaper already flipped this row
    # `running → failed` while we generated, this no-ops — so we don't re-stamp it and, crucially,
    # don't emit a *second* failure notification for a job the reaper already announced.
    # Stamp a TTL on failure too so the reaper's expiry sweep eventually cleans the row up.
    terminated = await session.execute(
        ExportJob.live_update()
        .where(col(ExportJob.id) == job_id, col(ExportJob.status) == ExportJobStatus.RUNNING)
        .values(
            status=ExportJobStatus.FAILED,
            error=message,
            expires_at=datetime.now(UTC) + timedelta(seconds=get_settings().export_job_ttl_seconds),
        )
    )
    if terminated.rowcount != 0:  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
        await _announce(
            session,
            job_id=job_id,
            requested_by_id=failed.requested_by_id,
            evaluation_id=failed.evaluation_id,
            evaluation_group_id=failed.evaluation_group_id,
            template=failed.template,
            export_format=failed.format,
            created_at=failed.created_at,
            succeeded=False,
            error=message,
        )
    await session.commit()
    logger.warning("exports.job.failed", job_id=str(job_id), error=str(exc))
    return {"status": ExportJobStatus.FAILED.value}


def _failure_message(exc: Exception) -> str:
    """A safe, requester-facing failure reason — never upstream/DB internals."""
    if isinstance(exc, _ExportJobError):
        return str(exc)
    if isinstance(exc, APIError):
        # NotFoundError (target no longer visible/live) is the expected detached-scope case.
        return "The export target is no longer available."
    return "Export generation failed."


@shared_task(name="app.core.exports.tasks.reap_expired_export_jobs")
def reap_expired_export_jobs() -> dict[str, int]:
    """Delete lapsed export files and recover stuck jobs.

    Two sweeps in one tick:

    - **Expiry:** any live job past `expires_at` (only `ready`/`failed` carry one) has its
      file deleted and its row soft-deleted — finished files AND recorded failures are
      eventually cleaned up.
    - **Stuck:** any `pending`/`running` job older than `export_stuck_ttl_seconds` (worker
      crash, or the broker dropped the message so no `acks_late` redelivery fires) is flipped
      to `failed` — it carries no `expires_at`, so the expiry sweep alone would leave it wedged
      and the download 409-ing forever. Stamped with a TTL so the next expiry sweep reaps it.

    Idempotent: a re-run (or overlapping tick) re-selects nothing already soft-deleted / failed.
    No retry — the next beat tick is the recovery path. Storage delete is best-effort per job so
    one missing/unresolvable file can't stall the sweep.
    """
    return asyncio.run(_reap_expired_export_jobs())


async def _reap_expired_export_jobs() -> dict[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    reaped = 0
    async with async_session_scope() as session:
        result = await session.execute(
            ExportJob.live_select().where(
                col(ExportJob.expires_at).is_not(None),
                col(ExportJob.expires_at) < now,
            )
        )
        jobs = list(result.scalars().all())
        storage = get_export_storage()
        for job in jobs:
            # Same best-effort file delete + soft-delete as the manual delete endpoint.
            await soft_delete_export_job(session, job, storage, by_id=None)
            reaped += 1

        # Stuck-job sweep: fail pending/running rows too old to still be in flight (see the task
        # docstring). A single conditional UPDATE (not select-then-mutate) so it is atomic — a job
        # that finishes (→ `ready`) between this tick and its commit no longer matches the
        # `status IN (pending, running)` predicate and is NOT clobbered; the worker's finalise CAS
        # is the mirror guard on the other side of that race. Stamp a TTL so the expiry sweep
        # (next tick) cleans the failed rows up.
        stuck_cutoff = now - timedelta(seconds=settings.export_stuck_ttl_seconds)
        timeout_message = "Export timed out."
        # `RETURNING` the affected rows keeps the fail-transition a single atomic statement (no
        # select-then-mutate TOCTOU with the worker's finalize CAS) while still handing us the rows
        # to notify. `.format` is a column label on the Row, not str.format.
        stuck_result = await session.execute(
            ExportJob.live_update()
            .where(
                col(ExportJob.status).in_((ExportJobStatus.PENDING, ExportJobStatus.RUNNING)),
                col(ExportJob.created_at) < stuck_cutoff,
            )
            .values(
                status=ExportJobStatus.FAILED,
                error=timeout_message,
                expires_at=now + timedelta(seconds=settings.export_job_ttl_seconds),
            )
            .returning(
                col(ExportJob.id),
                col(ExportJob.requested_by_id),
                col(ExportJob.evaluation_id),
                col(ExportJob.evaluation_group_id),
                col(ExportJob.template),
                col(ExportJob.format),
                col(ExportJob.created_at),
            )
        )
        stuck_rows = stuck_result.all()
        failed_stuck = len(stuck_rows)
        for row in stuck_rows:
            # `_announce` isolates each row in a SAVEPOINT + swallows, so one row's notify failure
            # can't abort the atomic status UPDATE or the other rows. A skipped row is never
            # re-announced (the sweep won't re-select a now-`failed` row).
            await _announce(
                session,
                job_id=row.id,
                requested_by_id=row.requested_by_id,
                evaluation_id=row.evaluation_id,
                evaluation_group_id=row.evaluation_group_id,
                template=row.template,
                export_format=row.format,
                created_at=row.created_at,
                succeeded=False,
                error=timeout_message,
            )

        await session.commit()
    if reaped or failed_stuck:
        logger.info("exports.reaper.swept", reaped=reaped, failed_stuck=failed_stuck)
    return {"reaped": reaped, "failed_stuck": failed_stuck}
