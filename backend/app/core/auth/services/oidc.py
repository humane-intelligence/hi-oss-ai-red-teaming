"""OIDC login orchestration — resolve the local user, mint the session JWT."""

import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import InvitationStatus
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.schemas import TokenResponse
from app.core.auth.services.invitations import accept_pending_invitations
from app.core.auth.services.jwt import mint_token_pair
from app.core.auth.services.providers import provider_trusts_unverified_email
from app.core.auth.services.roles import get_default_role
from app.core.auth.services.tokens import project_expired
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_user
from app.core.auth.services.users import get_user_by_email
from app.core.config import Settings
from app.core.email import send_email_best_effort
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.platform_settings.service import get_platform_settings

logger = get_logger(__name__)

_ACTIVATABLE = {UserStatus.PENDING, UserStatus.INVITED}
_ACCOUNT_ACTIVATED_TEMPLATE = "account_activated"


class MissingClaimError(Exception):
    """The IdP didn't return a claim we treat as required (e.g. `sub`)."""


class UnverifiedEmailError(Exception):
    """The IdP returned `email_verified=false` and this provider does not trust it.

    Treated as account-takeover risk: a session is not minted, the caller
    redirects with `#error=email_unverified`.
    """


class InactiveAccountError(Exception):
    """The resolved account cannot start a session.

    Covers a deactivated account, one soft-deleted after its identity was linked,
    and a live `invited` account behind an invitation that was since revoked or
    has lazily expired; the caller collapses all of these into one opaque
    `#error=account_inactive` redirect so the callback can't be used to probe
    which emails map to live accounts. Carries `user_id` — every raise site
    resolves an account before refusing it, so it's never unset — for the
    callback to audit *which* account a refused login targeted.
    """

    def __init__(self, message: str, *, user_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.user_id = user_id


class InviteOnlyError(Exception):
    """Provisioning a fresh account is refused — the platform is invite-only.

    Only the no-account branch raises: an existing account still logs in, and an
    `invited` / `pending` one still activates (it holds an invitation or predates
    the toggle). The IdP gate mirrors `register_user`'s, or Google login would be
    an open side door around it.
    """


async def _get_identity(session: AsyncSession, *, provider: str, subject: str) -> ProviderIdentity | None:
    result = await session.execute(
        ProviderIdentity.live_select()
        .where(col(ProviderIdentity.provider) == provider)
        .where(col(ProviderIdentity.subject) == subject),
    )
    return result.scalar_one_or_none()


def _optional_str_claim(claims: dict[str, Any], key: str) -> str | None:
    """Read an optional string claim, dropping it if present but the wrong type.

    A present-but-non-string claim must not reach `create_user` as-is: `User`
    (a `table=True` model) runs no validation on construction, so a non-string
    value raises an uncaught DB error on flush instead of a clean `#error=` redirect.
    """
    value = claims.get(key)
    return value if isinstance(value, str) else None


async def _activate(session: AsyncSession, user: User, *, email_verified: bool) -> None:
    """Promote a `pending` / `invited` account the IdP just vouched for.

    Refuses an `invited` account whose invitation was since revoked or has
    lazily expired — stricter than `register_user`'s `INVITED` branch, which
    reissues rather than blocks on an expired one; there's no accept link to
    re-mint here, so refusing is the only option. Clears any password already
    on the row: a `pending` self-registration stores the
    registrant's hash immediately, so without this an attacker who
    self-registers the victim's email keeps a live credential once the
    victim's Google login activates the account. Stamps `password_cleared_at`
    when it does — the only record of *why* the account is passwordless
    afterward, so `request_password_reset` can later tell "recovering from
    a clear" apart from "never had one, pure IdP account" and only reopen a
    local-login door for the former. Roles are left alone — an invited
    account keeps what it was provisioned with. Never call `session.refresh`
    here — it would expire the eager-loaded `roles` the token mint reads.
    """
    if user.status is UserStatus.INVITED:
        # `user.invitations` is the *platform*-scoped relationship only (`object_type
        # IS NULL`) — a group invite (`invite_to_group`) creates an INVITED user whose
        # only invitation is object-scoped, so this is `[]` for them even though they
        # were validly invited. Absent invitation falls through unchecked, same as
        # `register_user`'s INVITED branch — there's nothing to have been revoked.
        latest = user.invitations[0] if user.invitations else None
        if latest is not None:
            effective = project_expired(
                latest.status,
                latest.expires_at,
                pending=InvitationStatus.PENDING,
                expired=InvitationStatus.EXPIRED,
            )
            if effective is not InvitationStatus.PENDING:
                raise InactiveAccountError(f"account {user.id}'s invitation is no longer pending", user_id=user.id)

    had_password = user.password is not None
    user.status = UserStatus.ACTIVE
    user.password = None
    if had_password:
        user.password_cleared_at = datetime.now(UTC)
    if email_verified and user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
    session.add(user)
    await session.flush()
    await accept_pending_invitations(session, user.id)
    await send_email_best_effort(
        session,
        _ACCOUNT_ACTIVATED_TEMPLATE,
        user.email,
        {"user_name": user.first_name or user.email, "password_cleared": had_password},
    )


async def _resolve_user(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    claims: dict[str, Any],
) -> User:
    """Find (or provision) the local account behind an IdP identity.

    Returns the user with `roles` loaded — every branch goes through a loader
    that populates them, which `mint_token_pair` requires.
    """
    identity = await _get_identity(session, provider=provider, subject=subject)
    if identity is not None:
        try:
            return await get_user(session, identity.user_id)
        except NotFoundError as exc:
            raise InactiveAccountError(
                f"account behind identity {identity.id} no longer exists", user_id=identity.user_id
            ) from exc

    user = await get_user_by_email(session, email)
    if user is None:
        if (await get_platform_settings(session)).invite_only:
            raise InviteOnlyError("self-serve provisioning is disabled; the platform is invite-only")
        try:
            # SAVEPOINT: a concurrent first login for the same *email* (e.g. a double
            # click) may win the insert; `create_user` raises `ConflictError` on the
            # bare partial-unique-email violation with no savepoint of its own, which
            # would otherwise poison this whole transaction for the loser.
            async with session.begin_nested():
                user = await create_user(
                    session,
                    email=email,
                    roles=[await get_default_role(session)],
                    first_name=_optional_str_claim(claims, "given_name"),
                    last_name=_optional_str_claim(claims, "family_name"),
                    email_verified=email_verified,
                    status=UserStatus.ACTIVE,
                )
        except ConflictError:
            user = await get_user_by_email(session, email)
            if user is None:
                raise

    if user.status in _ACTIVATABLE:
        await _activate(session, user, email_verified=email_verified)
    elif user.status is not UserStatus.ACTIVE:
        # Refuse before linking: a deactivated account must not collect an
        # identity row on the way to being rejected. Runs on every path that
        # resolves `user` above, including the `ConflictError` race recovery —
        # a freshly created user is always `ACTIVE`, so this is a no-op there.
        raise InactiveAccountError(f"account {user.id} is {user.status.value}", user_id=user.id)

    try:
        # SAVEPOINT: a concurrent first login for the same subject may win the
        # insert; adopting its row beats failing a login on the unique index.
        async with session.begin_nested():
            session.add(ProviderIdentity(provider=provider, subject=subject, user_id=user.id))
            await session.flush()
    except IntegrityError:
        # This table has exactly one unique constraint, `(provider, subject)`, so a row
        # existing now is strong (not certain) evidence this was that race rather than an
        # unrelated violation — log either way, since the swallow itself leaves no other
        # signal to tell a benign double-click from a real constraint problem.
        if await _get_identity(session, provider=provider, subject=subject) is None:
            raise
        logger.info("auth.oidc.identity_race_recovered", provider=provider, subject=subject)
    return user


async def finalize_login(
    session: AsyncSession,
    *,
    provider: str,
    claims: dict[str, Any],
    settings: Settings,
) -> TokenResponse:
    """Resolve the user behind the id_token claims and return a bearer token pair.

    `claims` is the validated `userinfo` payload from authlib's
    `authorize_access_token` — signature, iss, aud, exp, and nonce have already
    been checked there.

    First login provisions: an unknown `(provider, sub)` links to the account
    owning the claimed email, or to a fresh `active` account when no account has
    it — unless the platform is invite-only, which refuses the fresh-account arm
    (and only that arm). Linking also activates a `pending` / `invited` account —
    the IdP's verification is at least as strong as our own email loop — keeping
    whatever roles it was invited with.

    Raises:
        MissingClaimError: `sub` or `email` is absent, non-string, or blank after
            stripping.
        UnverifiedEmailError: The provider reports the email unverified and is not trusted.
        InactiveAccountError: The resolved account is deactivated or gone.
        InviteOnlyError: No account owns the email and the platform is invite-only.
    """
    subject = claims.get("sub")
    email = claims.get("email")
    # `sub` gets no strip/normalize step (nothing downstream collapses distinct values
    # to the same one the way email-casing does), so its emptiness check runs here,
    # unlike email's — which must run on the *stripped* value below, not the raw one:
    # a claim of `"   "` is a truthy, non-empty `str` that would otherwise sail through
    # this guard and only turn into `""` on the next line.
    if not isinstance(subject, str) or not subject or not isinstance(email, str):
        raise MissingClaimError("id_token is missing required claim 'sub' or 'email'")
    # Postgres's default collation is case-sensitive (same reasoning as
    # `schemas.NormalizedEmail`) — an IdP-supplied claim goes through the same
    # canonicalisation every other entry point applies, or a differently-cased
    # claim (Google preserves the case a Workspace admin set) creates a
    # duplicate account instead of matching the existing one.
    email = email.strip().lower()
    if not email:
        raise MissingClaimError("id_token is missing required claim 'sub' or 'email'")

    # Not a bare `bool(...)`: Google sends a real boolean, but `providers.py`'s
    # registry is generic across IdPs, and some (Cognito, ADFS, Okta) report this
    # claim as the JSON string "false" — which `bool("false")` reads as `True`.
    raw_email_verified = claims.get("email_verified", False)
    email_verified = (
        raw_email_verified
        if isinstance(raw_email_verified, bool)
        else str(raw_email_verified).strip().lower() == "true"
    )
    if not email_verified and not provider_trusts_unverified_email(provider):
        raise UnverifiedEmailError(
            f"id_token email is not verified by provider {provider}",
        )

    user = await _resolve_user(
        session,
        provider=provider,
        subject=subject,
        email=email,
        email_verified=email_verified,
        claims=claims,
    )
    if user.status is not UserStatus.ACTIVE:
        raise InactiveAccountError(f"account {user.id} is {user.status.value}", user_id=user.id)

    logger.info("auth.oidc.login_succeeded", user_id=str(user.id), provider=provider)
    return mint_token_pair(user, provider=provider, settings=settings)
