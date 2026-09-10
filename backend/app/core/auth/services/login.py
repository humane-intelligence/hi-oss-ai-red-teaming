"""Email + password login.

Uniform 401 on every failure mode (unknown email, inactive account,
passwordless account, wrong password, over-cap password) to defeat user
enumeration via the response body. Login enforces no length policy — an empty
password is just a wrong credential, and an over-cap one is rejected before the
argon2 verify (to bound work) as the same 401, never a 422 leaking the policy.
Brute-force / timing-based enumeration is expected to be handled at the edge
(rate limiter), not here.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.password_policy import MAX_PASSWORD_LENGTH
from app.core.auth.services.passwords import hash_password
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.users import get_user_by_email
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger

logger = get_logger(__name__)

_INVALID_CREDENTIALS = "Invalid email or password."

# Pre-computed at import so the no-user / no-password branches still spend an
# argon2 verify worth of wall-time before returning 401 — closes the timing
# side-channel that would otherwise let an attacker enumerate accounts.
_DUMMY_HASH = hash_password("dummy-for-constant-time-verification")


async def authenticate_credentials(session: AsyncSession, *, email: str, password: str) -> User:
    """Resolve `email` + `password` to an ACTIVE `User`, or raise `UnauthorizedError`.

    Delegates the lookup to `get_user_by_email`, which eager-loads live
    `User.roles` so `mint_token_pair` can read them without a lazy load
    (which would crash under an async session).
    """
    if len(password) > MAX_PASSWORD_LENGTH:
        logger.info("auth.login.failed", reason="oversized_password")
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    user = await get_user_by_email(session, email)

    if user is None:
        verify_password(password, _DUMMY_HASH)
        logger.info("auth.login.failed", reason="unknown_email")
        raise UnauthorizedError(_INVALID_CREDENTIALS)
    if user.status != UserStatus.ACTIVE:
        verify_password(password, user.password or _DUMMY_HASH)
        logger.info("auth.login.failed", reason="inactive", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_CREDENTIALS)
    if user.password is None:
        verify_password(password, _DUMMY_HASH)
        logger.info("auth.login.failed", reason="passwordless", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_CREDENTIALS)
    if not verify_password(password, user.password):
        logger.info("auth.login.failed", reason="wrong_password", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    logger.info("auth.login.succeeded", user_id=str(user.id), provider="local")
    return user
