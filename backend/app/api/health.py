"""Liveness and readiness probes — top-level, unversioned endpoints."""

import asyncio
from typing import Literal

import asyncpg
from fastapi import APIRouter
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.dependencies import SettingsDep
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])

READINESS_TIMEOUT_SECONDS = 2


class CheckResult(BaseModel):
    """Outcome of a single readiness dependency check."""

    ok: bool
    error: str | None = None


class ReadinessResponse(BaseModel):
    """Aggregate readiness payload — one entry per checked dependency."""

    status: Literal["ok", "unavailable"]
    checks: dict[str, CheckResult]


@router.get("/health")
def healthcheck() -> dict[str, str]:
    """Liveness probe.

    Always returns 200 as long as the process can serve a request. Does not
    touch any dependency — use `/ready` for that.
    """
    return {"status": "ok"}


async def _check_postgres(settings: Settings) -> CheckResult:
    """Probe Postgres by opening a fresh connection and issuing ``SELECT 1``.

    Uses a short timeout so an unhealthy database can't stall the readiness
    response. Bypasses the application pool on purpose — pooled errors
    would mask a failed first connect from a freshly-started replica.

    Args:
        settings: Application settings carrying Postgres connection details.

    Returns:
        A `CheckResult` with ``ok=True`` on success, otherwise ``ok=False``
        and a stringified exception in ``error``.
    """
    try:
        conn = await asyncpg.connect(
            host=settings.database_host,
            port=settings.database_port,
            user=settings.database_user,
            password=settings.database_password,
            database=settings.database_name,
            timeout=READINESS_TIMEOUT_SECONDS,
        )
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning("readiness_check_failed", check="postgres", exc_info=exc)
        return CheckResult(ok=False, error=str(exc))
    return CheckResult(ok=True)


async def _check_redis(settings: Settings) -> CheckResult:
    """Probe Redis with a ``PING``.

    Uses a short connect + socket timeout so an unhealthy Redis can't stall
    the readiness response. The client is always closed, even on failure.

    Args:
        settings: Application settings carrying the Redis URL.

    Returns:
        A `CheckResult` with ``ok=True`` on success, otherwise ``ok=False``
        and a stringified exception in ``error``.
    """
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=READINESS_TIMEOUT_SECONDS,
        socket_timeout=READINESS_TIMEOUT_SECONDS,
    )
    try:
        await client.ping()
    except Exception as exc:
        logger.warning("readiness_check_failed", check="redis", exc_info=exc)
        return CheckResult(ok=False, error=str(exc))
    finally:
        await client.aclose()
    return CheckResult(ok=True)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    """Readiness probe — checks every dependency required to serve traffic.

    Runs each dependency check **concurrently** with a short per-check
    timeout, so an unhealthy backend cannot block the response.

    ### Status codes

    * **200 OK** — every dependency answered successfully.
    * **503 Service Unavailable** — at least one dependency failed; inspect
      the `checks` map for the offending key and its `error` string.

    Suitable as a Kubernetes `readinessProbe`. Use `/health` for liveness —
    this endpoint will fail fast on a dependency outage and should not be
    treated as a liveness signal.
    """
    postgres_check, redis_check = await asyncio.gather(_check_postgres(settings), _check_redis(settings))
    checks = {"postgres": postgres_check, "redis": redis_check}
    all_ok = all(c.ok for c in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if all_ok else "unavailable",
        checks=checks,
    )
