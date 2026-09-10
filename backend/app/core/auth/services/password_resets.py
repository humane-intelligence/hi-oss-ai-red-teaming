"""Password reset lifecycle — issue, lookup, confirm.

Mirrors `invitations.py`: raw token only in the outbound mail, sha256 hex in
the DB, `SELECT ... FOR UPDATE` to serialise concurrent submissions.

Two request-side entry points with opposite error contracts and opposite
passwordless handling: self-service (`request_password_reset`) no-ops on an
unknown/inactive/pure-IdP account but lets a recovering one through
(`password_cleared_at` set); the admin-triggered path
(`assert_password_reset_allowed`) hard-errors on all of those, recovery
included.

The self-service path is also the throttled one — an admin acting deliberately
is not the flood the cooldown exists for, though their sends do fill the window
the next self-service request measures. That path holds the account row locked
across the check-and-issue, so the throttle survives parallel requests.
"""

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import PasswordResetToken
from app.core.auth.models import PasswordResetTokenStatus
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.password_policy import PasswordPolicy
from app.core.auth.password_policy import validate_password
from app.core.auth.services.passwords import hash_password
from app.core.auth.services.session_revocation import revoke_user_sessions
from app.core.auth.services.tokens import generate_raw_token
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.tokens import project_expired
from app.core.auth.services.users import get_user
from app.core.auth.services.users import get_user_by_email
from app.core.config import get_settings
from app.core.email import send_email
from app.core.exceptions import ConflictError
from app.core.exceptions import GoneError
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.platform_settings.models import PlatformSettings
from app.core.platform_settings.service import get_platform_settings

_TEMPLATE_NAME = "password_reset"

logger = get_logger(__name__)


def _build_reset_url(raw_token: str) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}/password-reset/confirm?token={raw_token}"


async def _revoke_pending_resets(session: AsyncSession, user_id: UUID) -> None:
    """Revoke this user's PENDING reset tokens with `SELECT ... FOR UPDATE`.

    Serialises concurrent re-issues — without the lock two transactions could
    both load the same PENDING rows, revoke them, and each insert a fresh one,
    ending up with multiple live tokens for the same account.
    """
    result = await session.execute(
        PasswordResetToken.live_select()
        .where(col(PasswordResetToken.user_id) == user_id)
        .where(col(PasswordResetToken.status) == PasswordResetTokenStatus.PENDING)
        .with_for_update(),
    )
    now = datetime.now(UTC)
    for row in result.scalars().all():
        row.status = PasswordResetTokenStatus.REVOKED
        row.revoked_at = now
        session.add(row)
    await session.flush()


def _ineligible_reason(user: User) -> str | None:
    """Why this account can't be sent a self-service reset link, or None to proceed.

    A cleared password (`password_cleared_at`, set when Google login took over) still counts as
    eligible: that account has a local credential to restore. One that never had one does not.
    """
    if user.status is not UserStatus.ACTIVE:
        return "inactive"
    if user.password is None and user.password_cleared_at is None:
        return "passwordless"
    return None


async def _throttle_reason(session: AsyncSession, platform: PlatformSettings, user_id: UUID) -> str | None:
    """Return why this account may not be sent another link right now, or None to proceed.

    The window is derived from the account's own reset tokens — one token is one mail — so
    throttling needs no separate counter store. Admin-triggered resets land in the same window
    on purpose: the mail arrives in the same inbox, so it is the same flood.

    The limit is per account, deliberately not aggregate: a caller holding a list of addresses
    can still mail each of them once per cooldown. This bounds per-inbox volume, not total
    outbound — that needs a limiter at the edge, which the stack does not have.

    Reads the window, nothing more: serialising concurrent callers is the caller's job (see
    `request_password_reset`, which holds the account row locked around this). Without that lock
    a burst of parallel requests all measure the same empty window and all send — and the lock
    inside `_revoke_pending_resets` does not help, since a waiter's statement snapshot predates
    the winner's insert and so cannot see the token it just issued.

    Every timestamp in the comparison comes from the database clock, `created_at` included, so
    the window can't be skewed by the app process disagreeing with the server it reads from.
    """
    window = await session.execute(
        PasswordResetToken.live_select()
        .with_only_columns(func.count(), func.max(col(PasswordResetToken.created_at)), func.now())
        .where(col(PasswordResetToken.user_id) == user_id)
        .where(col(PasswordResetToken.created_at) > func.now() - timedelta(hours=24)),
    )
    issued, latest, now = window.one()

    if issued >= platform.password_reset_max_per_day:
        return "daily_cap"
    if latest is not None and latest > now - timedelta(seconds=platform.password_reset_cooldown_seconds):
        return "cooldown"
    return None


@dataclass(frozen=True, slots=True)
class PasswordResetEmailSpec:
    """The reset mail an issued token produced; the caller dispatches it.

    The reset URL is the raw token's only escape route and rides `secret_context`
    so it never hits the DB — the spec must therefore be sent or dropped within
    the request that produced it.
    """

    template: str
    to: str
    context: dict[str, Any]
    secret_context: dict[str, Any]


def assert_password_reset_allowed(user: User) -> None:
    """Reject an admin-triggered reset for an account that could not act on the link.

    Unlike `request_password_reset`, refuses every passwordless account here,
    recovery or not — an admin already has the user list, so silence would only
    hide whether the mail went out.

    Raises:
        ConflictError: The account is not `active`, or has no password to reset
            (it signs in through an identity provider).
    """
    if user.status is not UserStatus.ACTIVE:
        raise ConflictError(f"User is {user.status.value}; only an active account can be sent a password reset.")
    if user.password is None:
        raise ConflictError("User has no password to reset; the account signs in through an identity provider.")


async def issue_password_reset(
    session: AsyncSession, user: User, *, triggered_by_admin: bool = False
) -> PasswordResetEmailSpec:
    """Revoke `user`'s pending reset tokens, mint a fresh one, and return the mail to send.

    Deliberately does not send: a bulk caller must be able to defer every mail
    until after its own transaction commits, so that a row rolled back with its
    savepoint leaves no queued task pointing at a vanished token.

    Callers own the eligibility check — this issues a token for whatever user it
    is handed; `request_password_reset` and `assert_password_reset_allowed` each
    apply their own rule before calling this (see their docstrings).

    Args:
        session: Async DB session bound to the request.
        user: Account to issue the token for; eligibility is the caller's problem.
        triggered_by_admin: Rewrites the mail for a reset the recipient did not ask
            for. A user who never clicked "forgot password" must not be told they
            requested one — that is what trains people to click phishing links.
    """
    await _revoke_pending_resets(session, user.id)

    settings = get_settings()
    raw_token = generate_raw_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.password_reset_ttl_hours)
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    session.add(reset)
    await session.flush()

    return PasswordResetEmailSpec(
        template=_TEMPLATE_NAME,
        to=user.email,
        context={
            "recipient_name": user.full_name,
            "expires_at": expires_at,
            "triggered_by_admin": triggered_by_admin,
        },
        secret_context={"reset_url": _build_reset_url(raw_token)},
    )


async def request_password_reset(session: AsyncSession, *, email: str) -> None:
    """Issue a reset token for an ACTIVE user; silent no-op otherwise.

    Also doubles as "set a password" for an account recovering from an OIDC-triggered
    clear (`password_cleared_at` set) — a pure IdP-only account gets no such door.
    Always-204 and a uniform outcome across the no-op branches keep the response
    from leaking which emails are registered; each branch still logs its own reason
    for server-side observability.

    Throttling (`_throttle_reason`) is one of those silent branches rather than a 429: the
    window only exists for accounts that exist, so a distinct status would answer the very
    question the always-204 contract refuses. It runs under the account row lock, so parallel
    requests queue instead of all measuring the same empty window — a cap only one caller at a
    time can read is a cap a script can spend twenty sockets to ignore.
    """
    existing = await get_user_by_email(session, email)
    if existing is None:
        logger.info("auth.password_reset.requested", reason="unknown_email")
        return
    # Cheap pre-check on the unlocked read keeps the lock off the common no-op paths; the same
    # conditions are re-checked below, under the lock, because those are the ones that decide.
    ineligible = _ineligible_reason(existing)
    if ineligible is not None:
        logger.info("auth.password_reset.requested", reason=ineligible, user_id=str(existing.id))
        return

    try:
        user = await get_user(session, existing.id, for_update=True)
    except NotFoundError:
        # Soft-deleted between the unlocked lookup and the lock — a vanished account is just
        # another silent branch: a 404 from an always-204 endpoint would leak that the email
        # was registered. Same discipline as `resend_verification`.
        logger.info("auth.password_reset.requested", reason="vanished", user_id=str(existing.id))
        return
    ineligible = _ineligible_reason(user)
    if ineligible is not None:
        logger.info("auth.password_reset.requested", reason=f"{ineligible}_after_lock", user_id=str(user.id))
        return

    platform = await get_platform_settings(session)
    throttled = await _throttle_reason(session, platform, user.id)
    if throttled is not None:
        logger.info("auth.password_reset.requested", reason=f"throttled_{throttled}", user_id=str(user.id))
        return

    spec = await issue_password_reset(session, user)
    await send_email(session, spec.template, spec.to, spec.context, secret_context=spec.secret_context)
    logger.info("auth.password_reset.requested", reason="issued", user_id=str(user.id))


async def confirm_password_reset(session: AsyncSession, *, raw_token: str, password: SecretStr) -> None:
    """Hash the new password onto the user, mark the token USED, and end their sessions.

    `SELECT ... FOR UPDATE` serialises concurrent confirms on the same token; the
    second waits, reads the now-`USED` status, and gets a `GoneError`.

    Sessions are revoked because a password change whose whole point may be "someone
    else has my credentials" must not leave that someone's access token working until
    its own `exp`. It runs before the caller's commit, so a failed commit logs the user
    out without changing the password rather than the reverse; a Redis failure
    propagates and rolls the confirm back with the token still PENDING, leaving the
    link usable for a retry.

    Raises:
        NotFoundError: Token does not match any live reset row.
        GoneError: Token is no longer PENDING (used / revoked / expired), or the
            user has been soft-deleted or deactivated between request and confirm.
    """
    token_hash = hash_token(raw_token)
    locked = await session.execute(
        PasswordResetToken.live_select().where(col(PasswordResetToken.token_hash) == token_hash).with_for_update(),
    )
    reset = locked.scalar_one_or_none()
    if reset is None:
        raise NotFoundError("Password reset token not found.")

    effective = project_expired(
        reset.status,
        reset.expires_at,
        pending=PasswordResetTokenStatus.PENDING,
        expired=PasswordResetTokenStatus.EXPIRED,
    )
    if effective is not PasswordResetTokenStatus.PENDING:
        raise GoneError(f"Password reset token is {effective.value}.")

    user = await session.get(User, reset.user_id)
    if user is None or user.deleted_at is not None or user.status is not UserStatus.ACTIVE:
        raise GoneError("Password reset is no longer available for this account.")

    now = datetime.now(UTC)
    policy = PasswordPolicy.from_settings(await get_platform_settings(session))
    validate_password(
        password.get_secret_value(), identity=[user.email, user.first_name, user.last_name], policy=policy
    )
    user.password = hash_password(password.get_secret_value())
    session.add(user)

    reset.status = PasswordResetTokenStatus.USED
    reset.used_at = now
    session.add(reset)
    await session.flush()

    await revoke_user_sessions(user.id)
    logger.info("auth.password_reset.confirmed", user_id=str(user.id))
