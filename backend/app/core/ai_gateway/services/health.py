"""Manual per-model endpoint health check.

`start_health_check` flips the row to `checking`; the Celery task then calls
`run_health_check`, which loops a 1-token probe with backoff until the endpoint
answers or the wake-deadline passes (waiting out a scale-to-zero cold start),
then CAS-writes the outcome so a superseded run can't clobber a newer check.
"""

import asyncio
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.dispatch import dispatch_capability_probe
from app.core.ai_gateway.dispatch import dispatch_health
from app.core.ai_gateway.enums import HealthCheckStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import WarmupStatus
from app.core.ai_gateway.models import PROBED_CALL_FIELDS
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.providers.base import ModelProvider
from app.core.ai_gateway.services.ai_models import get_model
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def check_in_flight(model: AiModel, settings: Settings) -> bool:
    """True while `model`'s in-flight check is still owned by a task that could settle it.

    A check is abandoned once its owning task is guaranteed dead (its Celery `time_limit`) — the same
    horizon as the task, so a killed check is always immediately restartable with no dead window.
    Debounces the manual button only: the automatic start on create/PATCH supersedes an in-flight
    check rather than deferring to it, so it deliberately does not consult this.
    """
    if model.health_check_status is not HealthCheckStatus.CHECKING or model.last_health_check_at is None:
        return False
    stale_cutoff = datetime.now(UTC) - timedelta(seconds=settings.health_check_task_time_limit_seconds)
    return model.last_health_check_at > stale_cutoff


async def start_health_check(session: AsyncSession, model: AiModel) -> AiModel:
    """Mark `model` as a check in flight. Caller enqueues the task + owns the commit."""
    model.health_check_status = HealthCheckStatus.CHECKING
    model.last_health_check_at = datetime.now(UTC)
    session.add(model)
    await session.flush()
    # server-side `onupdate` expires `updated_at` on the UPDATE; reload it now so a later sync attribute
    # access (e.g. AiModelResponse.from_model) doesn't trigger a lazy refresh outside the async greenlet.
    await session.refresh(model, attribute_names=["updated_at"])
    return model


async def run_health_check(
    session: AsyncSession,
    settings: Settings,
    model_id: UUID,
    *,
    provider: ModelProvider | None = None,
) -> None:
    """Probe until the endpoint answers or the wake-deadline passes, then settle the row.

    A `STARTING` (cold) probe is retried every `health_check_backoff_seconds` up to
    `health_check_wake_deadline_seconds`; `READY`/`ERROR` settle immediately. The settle is CAS-guarded
    on `last_health_check_at` (the start stamp), so a newer check that superseded this one is not clobbered.

    An endpoint that *served* the text probe additionally gets a capability probe when the row
    claims image input; its outcome lands in `capability_mismatch` and never moves
    `health_check_status`. A 429 is reachable and alive but was not served, so it is not probed.
    That column is written only when the probe reached a conclusion — a passing probe clears a
    stale finding, a refusal records one, and anything else (dead endpoint, or a probe that hit a
    reachability fault) leaves it alone. `null` therefore keeps meaning "nothing contradicted the
    row", which it would not if a check that never asked wrote it.
    """
    model = await get_model(session, model_id, include_disabled=True)
    token = model.last_health_check_at  # CAS token: the start stamp we settle against
    # Release the read transaction before the (up to wake-deadline) probe loop, so the worker
    # connection doesn't sit idle-in-transaction while we sleep. `expire_on_commit=False` keeps
    # `model`'s attributes loaded for the probes; the CAS write below opens a fresh short txn.
    await session.commit()
    deadline = time.monotonic() + settings.health_check_wake_deadline_seconds
    alive, reason, served = False, "cold/deadline", False
    while True:
        status, probe_reason = await dispatch_health(model, settings, provider=provider)
        if status is WarmupStatus.READY:
            # `READY` with a reason is alive-but-throttled: reachable, but the text probe was not
            # actually served, so the capability probe has no baseline to be differential against.
            alive, reason, served = True, None, probe_reason is None
            break
        if status is WarmupStatus.ERROR:
            alive, reason = False, probe_reason
            break
        if time.monotonic() >= deadline:  # STARTING but out of time
            alive, reason = False, "cold/deadline"
            break
        await asyncio.sleep(settings.health_check_backoff_seconds)

    # Only worth asking an endpoint that actually served the text probe, and only about a capability
    # the row claims — there is nothing to contradict otherwise, and a throttled endpoint would bill
    # a second call to answer a question its 429 already refused.
    # Snapshot of the row fields the finding is evidence about, so the settle below can refuse to
    # attribute it to a row that has moved any of them since.
    probed = {field: getattr(model, field) for field in PROBED_CALL_FIELDS}
    declared = probed["input_modalities"]
    conclusive, mismatch = False, None
    if served and Modality.IMAGE in declared:
        conclusive, mismatch = await dispatch_capability_probe(model, settings, provider=provider)

    values: dict[str, object] = {
        "health_check_status": HealthCheckStatus.ALIVE if alive else HealthCheckStatus.DEAD,
        "last_health_reason": reason,
    }
    if alive:
        values["last_healthy_at"] = datetime.now(UTC)
    # CAS: only settle if this is still the same in-flight check (token unchanged since we started).
    result = await session.execute(
        update(AiModel).where(col(AiModel.id) == model_id, col(AiModel.last_health_check_at) == token).values(**values)
    )
    # The capability finding is written separately and additionally guarded on every value the
    # probe was made against. `update_model` clears the column when a PATCH moves any of them —
    # changing the declaration is the very remedy a finding prescribes — but that PATCH leaves
    # the CAS token alone, so without this the settle would re-stamp a finding about a call the
    # row no longer describes, and nothing but another manual check would clear it.
    capability_written = None
    if conclusive:
        capability_result = await session.execute(
            update(AiModel)
            .where(
                col(AiModel.id) == model_id,
                col(AiModel.last_health_check_at) == token,
                *(col(getattr(AiModel, field)) == value for field, value in probed.items()),
            )
            .values(capability_mismatch=mismatch)
        )
        capability_written = capability_result.rowcount > 0  # ty: ignore[unresolved-attribute]
    await session.commit()
    # Observability: the outcome lives only on a mutable row, so log it (no reaper watches this).
    # `rowcount` is on the CursorResult at runtime; execute() is typed as the base Result.
    if result.rowcount == 0:  # ty: ignore[unresolved-attribute]
        logger.info("health_check.superseded", model_id=str(model_id))
    else:
        logger.info(
            "health_check.settled",
            model_id=str(model_id),
            alive=alive,
            reason=reason,
            capability_mismatch=mismatch,
            # `None` when the probe reached no conclusion — it never ran, or ran and learned
            # nothing (`capability_probe.inconclusive` distinguishes those two). `False` when it
            # did conclude but the probed call moved mid-check, so the guard matched nothing and
            # the finding was dropped.
            capability_written=capability_written,
        )
