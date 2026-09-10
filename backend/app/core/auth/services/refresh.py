"""Refresh-token exchange.

Stateless on the token side — there is no server-side refresh-token store, but
an admin force-logout is honoured: a refresh token minted at or before the
user's revocation marker is rejected (see `session_revocation`), so rotation
cannot re-mint a fresh pair for a revoked session. An absolute session ceiling
*is* enforced statelessly via the immutable `auth_time` claim: rotation re-mints
with a fresh `exp`, so without
this a continuously-refreshed token would never expire. Identity is DB-backed:
the new pair is minted from the user's *current* roles and status, not from the
claims frozen in the refresh token, so a deactivated or re-permissioned account
is reflected at the next refresh.

Every failure mode collapses to the same `UnauthorizedError` so the endpoint
can't be used to probe which tokens map to live accounts.
"""

import time

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import UserStatus
from app.core.auth.schemas import TokenResponse
from app.core.auth.services.jwt import InvalidSessionTokenError
from app.core.auth.services.jwt import decode_refresh_jwt
from app.core.auth.services.jwt import mint_token_pair
from app.core.auth.services.session_revocation import is_revoked
from app.core.auth.services.users import get_user
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.exceptions import ServiceUnavailableError
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger

logger = get_logger(__name__)

_INVALID_REFRESH = "Invalid or expired refresh token."


async def refresh_session(session: AsyncSession, *, refresh_token: str, settings: Settings) -> TokenResponse:
    """Exchange a refresh token for a freshly minted access + refresh pair.

    Re-reads the user via `get_user` (which eager-loads live roles) so the new
    tokens carry current permissions; rejects deleted and non-ACTIVE accounts.
    Enforces the absolute session ceiling from `auth_time`, and preserves both
    `auth_time` and the original login `provider` from the refresh token's claims.

    Raises:
        UnauthorizedError: For any invalid/expired/wrong-`typ` token, a session
            past its absolute lifetime, an unknown (deleted) user, or a
            non-ACTIVE account.
    """
    secret = settings.session_jwt_secret.get_secret_value()
    algorithm = settings.session_jwt_algorithm

    try:
        identity = decode_refresh_jwt(refresh_token, secret=secret, algorithm=algorithm)
    except InvalidSessionTokenError as exc:
        logger.info("auth.refresh.failed", reason="invalid_token")
        raise UnauthorizedError(_INVALID_REFRESH) from exc

    # fail_open=False: a Redis failure here must surface, not silently let a revoked
    # session re-mint a fresh pair past its marker. Map the outage to 503 (transient
    # dependency) rather than a bare 500 so the client gets a retry signal.
    try:
        revoked = await is_revoked(identity.id, identity.issued_at, fail_open=False)
    except RedisError as exc:
        logger.warning("auth.refresh.revocation_unavailable", user_id=str(identity.id), exc_info=exc)
        raise ServiceUnavailableError("Session store unavailable; please retry.") from exc
    if revoked:
        logger.info("auth.refresh.failed", reason="revoked", user_id=str(identity.id))
        raise UnauthorizedError(_INVALID_REFRESH)

    # Tokens minted before `auth_time` existed are grandfathered: treat a missing
    # claim as "starts now", so the ceiling applies from this refresh forward.
    now = int(time.time())
    auth_time = identity.auth_time if identity.auth_time is not None else now
    if now - auth_time > settings.refresh_absolute_max_lifetime_seconds:
        logger.info("auth.refresh.failed", reason="absolute_lifetime_exceeded", user_id=str(identity.id))
        raise UnauthorizedError(_INVALID_REFRESH)

    try:
        user = await get_user(session, identity.id)
    except NotFoundError as exc:
        logger.info("auth.refresh.failed", reason="unknown_user", user_id=str(identity.id))
        raise UnauthorizedError(_INVALID_REFRESH) from exc

    if user.status != UserStatus.ACTIVE:
        logger.info("auth.refresh.failed", reason="inactive", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_REFRESH)

    logger.info("auth.refresh.succeeded", user_id=str(user.id), provider=identity.provider)
    return mint_token_pair(user, provider=identity.provider, settings=settings, auth_time=auth_time)
