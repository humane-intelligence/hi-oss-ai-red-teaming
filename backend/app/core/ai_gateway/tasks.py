"""Celery tasks for the ai_gateway (auto-discovered).

Both tasks reuse async services, so they wrap their bodies in `asyncio.run` with
`async_session_scope` — the exports-task pattern. `run_model_health_check` carries a
`time_limit` bounding the whole probe loop so a wedged check can't run forever.
"""

import asyncio
from uuid import UUID

from celery import shared_task

from app.core.ai_gateway.services.health import run_health_check
from app.core.ai_gateway.services.inactivity import alert_inactive_models
from app.core.config import get_settings
from app.workers.session import async_session_scope

_TIME_LIMIT = get_settings().health_check_task_time_limit_seconds


@shared_task(name="app.core.ai_gateway.tasks.run_model_health_check", time_limit=_TIME_LIMIT)
def run_model_health_check(model_id: str) -> None:
    """Run the (async) endpoint health check for `model_id` and settle the row.

    Idempotent: the settle is a CAS on `last_health_check_at` (the start stamp), so a retry or a
    duplicate delivery that runs after a newer check started finds no matching row and no-ops.
    """

    async def _run() -> None:
        settings = get_settings()
        async with async_session_scope() as session:
            await run_health_check(session, settings, UUID(model_id))

    asyncio.run(_run())


@shared_task(name="app.core.ai_gateway.tasks.check_model_inactivity")
def check_model_inactivity() -> dict[str, int]:
    """Alert admins about warmup-enabled models nobody has messaged lately.

    Idempotent: each alert is claimed by a guarded `UPDATE … WHERE inactivity_alerted_at
    IS NULL`, so a redelivery or an overlapping tick re-announces nothing. No retry
    policy — the next beat tick is the recovery path.
    """

    async def _run() -> int:
        async with async_session_scope() as session:
            # The sweep commits per alert itself — a mail queued against an uncommitted
            # row is dropped by the worker, so the commits cannot wait for the loop to end.
            return await alert_inactive_models(session)

    return {"alerted": asyncio.run(_run())}
