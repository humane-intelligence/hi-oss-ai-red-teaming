"""Self-service credential change on the caller's own account."""

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.auth.password_policy import MAX_PASSWORD_LENGTH
from app.core.auth.password_policy import PasswordPolicy
from app.core.auth.password_policy import validate_password
from app.core.auth.services.passwords import hash_password
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.session_revocation import revoke_user_sessions
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.platform_settings.service import get_platform_settings
from app.core.schemas import ProblemErrorItem

logger = get_logger(__name__)


class CurrentPasswordError(BadRequestError):
    """The supplied current password does not match — 400, field-addressable.

    Not a 401: the FE treats every 401 as an expired session and drops the token,
    which would log the user out over a typo. The `errors[]` entry mirrors
    `PasswordPolicyError` so a client maps the failure onto the field.
    """

    def __init__(self) -> None:
        detail = "Current password is incorrect."
        super().__init__(detail)
        self.errors = [
            ProblemErrorItem(loc=["body", "current_password"], msg=detail, type="current_password_incorrect")
        ]


async def change_own_password(
    session: AsyncSession, user: User, *, current_password: SecretStr, new_password: SecretStr
) -> None:
    """Verify the current password, hash the new one onto the user, and end their sessions.

    Change-only: a passwordless account (pure-IdP, or an OIDC activation cleared the
    password) is refused — the mailbox-proof reset flow stays the only door back to a
    local credential.

    Sessions are revoked for the same reason `confirm_password_reset` revokes them: a
    change whose motive may be "someone else has my credentials" must not leave that
    someone's access token working until its own `exp`. Revocation runs before the
    caller's commit, so a failed commit logs the user out without changing the
    password rather than the reverse.

    Raises:
        ConflictError: The account has no local password.
        CurrentPasswordError: The current password does not match.
        PasswordPolicyError: The new password fails a content rule.
    """
    # No status gate (unlike `confirm_password_reset`): deactivation revokes sessions,
    # so no live token should reach here for a non-ACTIVE account.
    if user.password is None:
        raise ConflictError("The account has no local password; use the password-reset flow instead.")
    secret = current_password.get_secret_value()
    # The length guard bounds the verification work before argon2 sees the input
    # (same rule as login); an over-cap current password is by definition wrong.
    if len(secret) > MAX_PASSWORD_LENGTH or not verify_password(secret, user.password):
        # A refusal on a valid token is the stolen-token probing signal — must not be invisible.
        logger.info("auth.password_change.refused", user_id=str(user.id))
        raise CurrentPasswordError
    policy = PasswordPolicy.from_settings(await get_platform_settings(session))
    validate_password(
        new_password.get_secret_value(), identity=[user.email, user.first_name, user.last_name], policy=policy
    )
    user.password = hash_password(new_password.get_secret_value())
    session.add(user)
    await session.flush()
    await revoke_user_sessions(user.id)
    logger.info("auth.password_change.confirmed", user_id=str(user.id))
