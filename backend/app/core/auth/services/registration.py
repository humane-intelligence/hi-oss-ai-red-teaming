"""Self-signup registration and email verification lifecycle.

Mirrors `app.core.auth.services.invitations`: a user-bound, hashed-token row
in `email_verifications` carries the one-time link state, and the raw token
exists only in memory long enough to compose the outgoing URL.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import EmailVerification
from app.core.auth.models import EmailVerificationStatus
from app.core.auth.models import InvitationStatus
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.password_policy import PasswordPolicy
from app.core.auth.password_policy import validate_password
from app.core.auth.services.invitations import accept_pending_invitations
from app.core.auth.services.invitations import reissue_platform_invitation
from app.core.auth.services.passwords import hash_password
from app.core.auth.services.roles import get_default_role
from app.core.auth.services.tokens import generate_raw_token
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.tokens import project_expired
from app.core.auth.services.users import get_user
from app.core.auth.services.users import get_user_by_email
from app.core.config import get_settings
from app.core.email import send_email
from app.core.email import send_email_best_effort
from app.core.exceptions import ForbiddenError
from app.core.exceptions import GoneError
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.platform_settings.service import get_platform_settings
from app.core.terms.service import record_signup_consent
from app.core.terms.service import resolve_signup_consent

logger = get_logger(__name__)

_VERIFICATION_TEMPLATE = "email_verification"
_ACCOUNT_ACTIVATED_TEMPLATE = "account_activated"


def _build_verify_url(raw_token: str) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}/register/verify?token={raw_token}"


async def _revoke_pending_verifications(session: AsyncSession, user_id: UUID) -> None:
    """Revoke this user's PENDING verifications.

    `SELECT ... FOR UPDATE` serialises concurrent re-registrations against the
    same user — without it, two transactions could both load the same PENDING
    rows, revoke them, and each insert a fresh PENDING, ending up with two
    live tokens.
    """
    result = await session.execute(
        EmailVerification.live_select()
        .where(col(EmailVerification.user_id) == user_id)
        .where(col(EmailVerification.status) == EmailVerificationStatus.PENDING)
        .with_for_update(),
    )
    now = datetime.now(UTC)
    for row in result.scalars().all():
        row.status = EmailVerificationStatus.REVOKED
        row.revoked_at = now
        session.add(row)
    await session.flush()


async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: SecretStr,
    first_name: str | None,
    last_name: str | None,
    consent_terms: bool = False,
    consent_emails: bool = False,
    terms_id: UUID | None = None,
) -> None:
    """Issue (or re-issue) an email-verification token for a self-signup.

    Always succeeds from the caller's POV — the endpoint maps every outcome to
    `202 Accepted` so the response does not reveal whether the email is in
    use. Concretely:

    * New email → create a `PENDING` user with the supplied password and the
      default `red_teamer` role, then issue a verification token.
    * Existing user in `PENDING` → revoke their live verifications and issue a
      fresh one. Password and names are *not* overwritten, so re-registration
      cannot hijack a half-completed signup.
    * Existing user in `INVITED` → an admin-provisioned account self-onboarding
      after its invite link expired or went unused. Re-issue the platform
      invitation (`reissue_platform_invitation`) — a fresh accept link to the
      account's own address — rather than setting a password here: an email-only
      request must never bind a credential to a pre-existing account. The
      supplied password / names are ignored, the account stays `INVITED`, and the
      email owner sets the password through `accept_invitation`. **Unless an admin
      revoked the invitation** — then this path would hand back the very link the
      revoke was meant to kill, so it is a silent no-op like the arm below.
    * Existing user in any other live status (`ACTIVE` / `INACTIVE`) → silent
      no-op (no DB write, no email).

    Consent is recorded on the new-account arm only, and only against the version the form
    rendered (`terms_id` — see `resolve_signup_consent`). The PENDING arm leaves it alone for
    the same reason it leaves the password and names alone — a re-registration must not
    rewrite a half-completed signup — and the other arms write nothing at all. An account
    that arrives without consent (or predates the current version) is caught by the
    acceptance gate at its next request, which is also the only path for the doors that
    have no form (OIDC first login, admin-provisioned accounts).

    Under invite-only mode (`PlatformSettings.invite_only`) every call raises
    `ForbiddenError` *before* any lookup or write — the refusal is uniform across
    emails, so the mode adds no enumeration signal. Invited accounts onboard via
    `accept_invitation`, never here; the INVITED re-issue arm below is likewise
    unreachable until the mode is lifted.

    Raises:
        ForbiddenError: invite-only mode is enabled — open self-signup is disabled.
        ConsentRequiredError: terms are published and the form declined them.
        StaleTermsVersionError: the form accepted a version that is no longer current.
    """
    platform = await get_platform_settings(session)
    if platform.invite_only:
        raise ForbiddenError("Self-registration is disabled; the platform is invite-only.")

    # Both guards run before the existence lookup so a refusal hinges on the payload alone,
    # not on whether the email exists — keeps registration enumeration-safe.
    consent_document = await resolve_signup_consent(session, consent_terms=consent_terms, terms_id=terms_id)
    validate_password(
        password.get_secret_value(),
        identity=[email, first_name, last_name],
        policy=PasswordPolicy.from_settings(platform),
    )

    existing = await get_user_by_email(session, email)

    if existing is None:
        role = await get_default_role(session)
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=hash_password(password.get_secret_value()),
            status=UserStatus.PENDING,
        )
        record_signup_consent(user, document=consent_document, consent_emails=consent_emails, now=datetime.now(UTC))
        try:
            # SAVEPOINT around the racing insert: an `IntegrityError` from a
            # concurrent registration for the same email rolls back only
            # this nested block, leaving the outer request transaction
            # intact.
            async with session.begin_nested():
                session.add(user)
                user.roles = [role]
                await session.flush()
        except IntegrityError:
            return
    elif existing.status is UserStatus.PENDING:
        # Refetch with FOR UPDATE so concurrent re-registrations for the same
        # email serialise — see `_revoke_pending_verifications`.
        user = await get_user(session, existing.id, for_update=True)
        # Status may have shifted between the unlocked read above and the
        # locking refetch; re-check before issuing a
        # fresh verification row against an already-completed account.
        if user.status is not UserStatus.PENDING:
            return
        await _revoke_pending_verifications(session, user.id)
    elif existing.status is UserStatus.INVITED:
        # Refetch with FOR UPDATE so a concurrent register / invite-accept on the
        # same account serialises, then re-check after the lock.
        user = await get_user(session, existing.id, for_update=True)
        if user.status is not UserStatus.INVITED:
            return
        # A revoked invitation must stay revoked: without this, anyone who knows the
        # address could re-mint the accept link the admin just killed, against an
        # account that still carries its invite-time roles. `get_user` eager-loads
        # `User.invitations` newest-first (the relationship's order_by), post-lock.
        latest = user.invitations[0] if user.invitations else None
        if latest is not None and latest.status is InvitationStatus.REVOKED:
            return
        # Do NOT set a password here. Binding a credential from an email-only
        # request would let an attacker plant a password that the victim's
        # verification click activates (account takeover, carrying preserved
        # invite-time roles). Re-issue the invitation instead: a fresh accept link
        # is mailed to the account's own address, and the email owner sets the
        # password via `accept_invitation`. The supplied password / names are
        # ignored, and the account stays INVITED until the link is used.
        await reissue_platform_invitation(session, user)
        return
    else:
        return

    await _issue_verification(session, user, ttl_hours=platform.email_verification_ttl_hours)


async def _issue_verification(session: AsyncSession, user: User, *, ttl_hours: int) -> None:
    """Insert a fresh PENDING verification row and mail its one-time link.

    `ttl_hours` comes from the platform settings (admin-tunable), not the raw env default.
    """
    raw_token = generate_raw_token()
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    verification = EmailVerification(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    session.add(verification)
    await session.flush()

    await send_email(
        session,
        _VERIFICATION_TEMPLATE,
        user.email,
        {"expires_at": expires_at},
        secret_context={"verify_url": _build_verify_url(raw_token)},
    )


async def resend_verification(session: AsyncSession, *, email: str) -> None:
    """Re-issue the verification link for a still-pending self-signup.

    Deliberately a silent no-op for every other outcome (unknown email, `ACTIVE` /
    `INACTIVE` / `INVITED`) — the endpoint maps everything to 202, so the response
    can't be used to enumerate accounts. An `INVITED` account's recovery path is
    `/auth/register` (invitation re-issue), never here: a verification token must
    not exist for an account that sets its credential through `accept_invitation`.
    Stays open under invite-only mode — a PENDING signup can only predate the flip
    (the mode refuses new ones), and the toggle governs new signups, not in-flight
    verifications.

    Every branch logs its own reason: the response is a uniform 202, so the log is
    the only place "the user says no mail arrived" can be answered. Mirrors
    `request_password_reset`; the email itself never reaches the log.
    """
    existing = await get_user_by_email(session, email)
    if existing is None:
        logger.info("auth.verification_resend.requested", reason="unknown_email")
        return
    if existing.status is not UserStatus.PENDING:
        logger.info("auth.verification_resend.requested", reason="not_pending", user_id=str(existing.id))
        return
    # Refetch with FOR UPDATE so concurrent resends / re-registrations / verifies
    # serialise, then re-check — the same discipline as `register_user`'s PENDING arm.
    try:
        user = await get_user(session, existing.id, for_update=True)
    except NotFoundError:
        # Soft-deleted between the unlocked lookup and the lock — a vanished account is
        # just another silent no-op: a 404 here would leak that the email was registered.
        logger.info("auth.verification_resend.requested", reason="vanished", user_id=str(existing.id))
        return
    if user.status is not UserStatus.PENDING:
        logger.info("auth.verification_resend.requested", reason="not_pending_after_lock", user_id=str(user.id))
        return
    await _revoke_pending_verifications(session, user.id)
    ttl_hours = (await get_platform_settings(session)).email_verification_ttl_hours
    await _issue_verification(session, user, ttl_hours=ttl_hours)
    logger.info("auth.verification_resend.requested", reason="issued", user_id=str(user.id))


async def verify_email(session: AsyncSession, *, raw_token: str) -> User:
    """Consume a verification token, mark the email verified, activate the user.

    `SELECT ... FOR UPDATE` serialises concurrent submissions of the same
    token; the second waits, reads the now-`VERIFIED` status, and raises
    `GoneError`.

    This path serves plain self-signup (a new email or a `PENDING` resend); an
    *invited* account onboards via `accept_invitation`, never here. A self-signup
    has no invitation of its own, but `invite_to_group` may have issued it a
    group-scoped invitation while it was still `PENDING`, so activation sweeps any
    live invitation to `ACCEPTED` (`accept_pending_invitations`) — a no-op when
    there is none.

    Raises:
        NotFoundError: Token does not match any live verification.
        GoneError: Verification is no longer PENDING, or the user vanished.
    """
    token_hash = hash_token(raw_token)
    locked = await session.execute(
        EmailVerification.live_select().where(col(EmailVerification.token_hash) == token_hash).with_for_update(),
    )
    verification = locked.scalar_one_or_none()
    if verification is None:
        raise NotFoundError("Verification token not found.")

    effective = project_expired(
        verification.status,
        verification.expires_at,
        pending=EmailVerificationStatus.PENDING,
        expired=EmailVerificationStatus.EXPIRED,
    )
    if effective is not EmailVerificationStatus.PENDING:
        raise GoneError(f"Verification is {effective.value}.")

    try:
        user = await get_user(session, verification.user_id, for_update=True)
    except NotFoundError as exc:
        raise GoneError("Verification user is no longer available.") from exc

    now = datetime.now(UTC)
    user.status = UserStatus.ACTIVE
    user.email_verified_at = now
    session.add(user)

    verification.status = EmailVerificationStatus.VERIFIED
    verification.verified_at = now
    session.add(verification)
    await session.flush()
    await session.refresh(user)

    # A self-signup carries no invitation of its own, but `invite_to_group` may
    # have issued it a group-scoped one while it was still PENDING; activation
    # consumes any live invitation so none lingers PENDING against the now-active
    # account. No-op for the common case (no invitation).
    await accept_pending_invitations(session, user.id)

    await send_email_best_effort(
        session,
        _ACCOUNT_ACTIVATED_TEMPLATE,
        user.email,
        {"user_name": user.first_name or user.email},
    )
    return user
