"""Per-user session revocation via a Redis "revoked-after" marker.

Auth is stateless JWT with no server-side session store, so a token stays valid
until its own ``exp``. Force-logout writes a per-user epoch to Redis; the auth
middleware and the refresh exchange reject any token not minted strictly after
it, which revokes every one of that user's live sessions at once. The marker's
TTL is an upper bound on the longest-lived outstanding token (the refresh TTL;
the 90d session ceiling used here is a validator-backed safe bound — see
``config.py``), so it self-expires once no pre-revocation token could still be
valid. An over-long TTL never yields a false positive: a post-revoke token has
``iat > marker`` regardless of how long the marker lingers.

Operational constraints (single-instance today):
- Marker and token ``iat`` both come from per-instance ``time.time()``; scaling
  to N app instances makes NTP an ops requirement, or add a forward margin.
- The read-path check fails **open**: a `RedisError` (connection or timeout)
  degrades to "not revoked" so a cache blip can't 401 all traffic. The refresh
  path opts out (``fail_open=False``) because it mints credentials — see
  `is_revoked`.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "auth:revoked_after:"

# Short timeouts: this sits on the auth hot path and the read check fails open,
# so a hung Redis should fail fast rather than stall every request (redis-py's
# 5s default would brown out the API for the duration of an outage).
_REDIS_TIMEOUT_SECONDS = 1

# Fail-open is invisible in logs alone; this surfaces a partial Redis outage on
# the /metrics dashboards (the fail-closed path already surfaces as a 503).
REVOCATION_CHECK_FAILED = Counter(
    "redteam_auth_revocation_check_failed_total",
    "Session-revocation checks that errored (Redis) and fell open to 'not revoked'.",
)


def _make_client(url: str) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
    )


# Populated by the app lifespan (`init_client`) with a pooled, process-wide
# client that lives for the whole process — the hot path (middleware on every
# authenticated request) reuses it instead of connecting per call. Left empty
# under the test harness, where `ASGITransport` skips lifespan and each call
# falls back to a short-lived client bound to the running loop (a `redis.asyncio`
# client binds to the loop that first uses it, so a shared one can't cross the
# suite's per-test loops).
_shared: dict[str, Redis] = {}


async def init_client(settings: Settings) -> None:
    """Build the process-wide pooled client. Called once from the app lifespan."""
    _shared["client"] = _make_client(settings.redis_url)


async def close_client() -> None:
    """Dispose the pooled client. Called from the app lifespan on shutdown."""
    client = _shared.pop("client", None)
    if client is not None:
        await client.aclose()


def _key(user_id: UUID) -> str:
    return f"{_KEY_PREFIX}{user_id}"


@asynccontextmanager
async def _redis() -> AsyncIterator[Redis]:
    """The pooled client when the lifespan set one up; else a short-lived one."""
    shared = _shared.get("client")
    if shared is not None:
        yield shared
        return
    client = _make_client(get_settings().redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def revoke_user_sessions(user_id: UUID) -> None:
    """Force-logout ``user_id``: mark every session minted up to now as revoked.

    Sets the marker to the current epoch with a TTL that outlives the longest
    possible token, so `is_revoked` rejects all of the user's current tokens
    until they would have expired anyway. Lets a Redis failure propagate — no
    caller (an admin force-logout or deactivation, a user's own completed
    password reset) may report success when the revocation did not land.
    """
    async with _redis() as client:
        await client.set(_key(user_id), int(time.time()), ex=get_settings().refresh_absolute_max_lifetime_seconds)


async def is_revoked(user_id: UUID, issued_at: int | None, *, fail_open: bool = True) -> bool:
    """True when ``user_id`` has a revocation marker at or after the token's ``iat``.

    A token counts as revoked unless it was minted strictly after the marker, so
    one issued in the same second as the force-logout is caught too (there is no
    ``jti`` for finer ordering). A token with no ``iat`` is never revoked.

    ``fail_open`` (default, the middleware/access path): a missing marker, a
    Redis error, or a corrupt marker all return ``False`` so a cache outage
    degrades to "no revocation" rather than locking every request out. The
    refresh path passes ``fail_open=False``: it mints a fresh pair, so a Redis
    failure there must **raise** — otherwise a revoked session could re-mint
    credentials with ``iat > marker`` and escape revocation permanently.
    """
    if issued_at is None:
        return False
    try:
        async with _redis() as client:
            marker = await client.get(_key(user_id))
        return marker is not None and issued_at <= int(marker)
    except (RedisError, ValueError) as exc:
        if not fail_open:
            raise
        REVOCATION_CHECK_FAILED.inc()
        logger.warning("auth.revocation.check_failed", user_id=str(user_id), exc_info=exc)
        return False
