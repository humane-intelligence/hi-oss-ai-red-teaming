"""finalize_login — claim validation, first-login provisioning, JWT mint."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

import app.core.auth.services.oidc as oidc_module
from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.oidc import InactiveAccountError
from app.core.auth.services.oidc import InviteOnlyError
from app.core.auth.services.oidc import MissingClaimError
from app.core.auth.services.oidc import UnverifiedEmailError
from app.core.auth.services.oidc import finalize_login
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_user_by_email
from app.core.auth.services.users import soft_delete_user
from app.core.auth.services.users import user_projection_options
from app.core.config import Settings
from app.core.email.models import OutboundEmail
from app.core.platform_settings.service import update_platform_settings

SECRET = "test-secret-comfortably-long-for-hs256"

CLAIMS = {
    "sub": "google-sub-123",
    "email": "ada@example.com",
    "email_verified": True,
    "given_name": "Ada",
    "family_name": "Lovelace",
}


def _settings() -> Settings:
    return Settings.model_construct(
        database_host="x",
        database_port=5432,
        database_user="x",
        database_password="x",
        database_name="x",
        redis_host="x",
        redis_port=6379,
        session_jwt_secret=SecretStr(SECRET),
        session_jwt_algorithm="HS256",
        session_ttl_seconds=3600,
        refresh_token_ttl_seconds=2_592_000,
    )


@pytest_asyncio.fixture
async def default_role(db_session: AsyncSession) -> Role:
    role = Role(name="red_teamer", display_name="Red Teamer", description="Red Teamer", permissions=[], is_default=True)
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _identities(session: AsyncSession, subject: str) -> list[ProviderIdentity]:
    result = await session.execute(ProviderIdentity.live_select().where(col(ProviderIdentity.subject) == subject))
    return list(result.scalars().all())


async def _make_invitation(
    session: AsyncSession,
    user_id: UUID,
    *,
    status: InvitationStatus = InvitationStatus.PENDING,
    expires_at: datetime | None = None,
) -> Invitation:
    invitation = Invitation(
        user_id=user_id,
        token_hash=hash_token(f"invite-{user_id}-{status.value}-{expires_at}"),
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=48)),
        status=status,
    )
    session.add(invitation)
    await session.flush()
    return invitation


async def _user_by_email(session: AsyncSession, email: str) -> User:
    """Fetch with `roles` eager-loaded — `finalize_login` returns only the `TokenResponse`,
    so its `User` is unreferenced once it returns and the session's weakly-referenced
    identity map can drop it before this query runs; a plain select would then construct a
    fresh instance with `roles` unloaded, and a sync `.roles` access crashes under async.
    """
    result = await session.execute(
        User.live_select().options(*user_projection_options()).where(col(User.email) == email)
    )
    return result.scalar_one()


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_mints_token_whose_claims_match_the_user(db_session: AsyncSession) -> None:
    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())
    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")

    assert response.expires_in == 3600
    assert response.refresh_token
    assert session.provider == "google"
    assert session.email == "ada@example.com"
    assert session.first_name == "Ada"
    assert session.last_name == "Lovelace"
    assert session.email_verified is True


@pytest.mark.integration
async def test_finalize_login_provisions_an_active_user_on_first_login(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None
    assert user.password is None
    assert [r.id for r in user.roles] == [default_role.id]
    # The minted token must be about the row we just persisted, not a throwaway id.
    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == user.id


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_invite_only_refuses_provisioning_an_unknown_email(db_session: AsyncSession) -> None:
    """The gate mirrors `register_user`'s — without it, Google login is an open side door."""
    await update_platform_settings(db_session, invite_only=True)

    with pytest.raises(InviteOnlyError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    assert await get_user_by_email(db_session, "ada@example.com") is None
    assert await _identities(db_session, "google-sub-123") == []


@pytest.mark.integration
async def test_finalize_login_invite_only_still_links_an_existing_active_account(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """Only the fresh-account arm is gated — an existing account keeps logging in."""
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.ACTIVE)
    await update_platform_settings(db_session, invite_only=True)

    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == existing.id
    assert len(await _identities(db_session, "google-sub-123")) == 1


@pytest.mark.integration
async def test_finalize_login_invite_only_still_activates_an_invited_account(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """An `invited` account holds an invitation — exactly whom the mode lets in."""
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INVITED)
    await _make_invitation(db_session, existing.id)
    await update_platform_settings(db_session, invite_only=True)

    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.id == existing.id
    assert user.status is UserStatus.ACTIVE


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_links_one_identity_row_per_idp_account(db_session: AsyncSession) -> None:
    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    identities = await _identities(db_session, "google-sub-123")
    assert len(identities) == 1
    assert identities[0].provider == "google"
    assert identities[0].user_id == (await _user_by_email(db_session, "ada@example.com")).id


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_reuses_the_same_account_on_repeat_login(db_session: AsyncSession) -> None:
    """The whole point of persisting identities: a stable `id` across sessions."""
    first = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())
    second = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    first_id = decode_session_jwt(first.access_token, secret=SECRET, algorithm="HS256").id
    second_id = decode_session_jwt(second.access_token, secret=SECRET, algorithm="HS256").id
    assert first_id == second_id
    assert len(await _identities(db_session, "google-sub-123")) == 1


@pytest.mark.integration
async def test_finalize_login_links_to_an_existing_active_account(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    existing = await create_user(
        db_session,
        email="ada@example.com",
        roles=[member_role],
        status=UserStatus.ACTIVE,
        email_verified=True,
        password=SecretStr("correct-horse-battery-staple"),
    )

    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == existing.id
    identities = await _identities(db_session, "google-sub-123")
    assert [i.user_id for i in identities] == [existing.id]
    # Linking must not touch the local credential.
    assert (await _user_by_email(db_session, "ada@example.com")).password is not None


@pytest.mark.integration
async def test_finalize_login_links_a_mixed_case_claim_to_the_existing_account(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """Postgres's default collation is case-sensitive; Google preserves whatever case a
    Workspace admin set on the address, so the claim must be canonicalised before the
    lookup or this creates a second account instead of linking the existing one.
    """
    existing = await create_user(
        db_session,
        email="ada@example.com",
        roles=[member_role],
        status=UserStatus.ACTIVE,
        email_verified=True,
    )
    claims = {**CLAIMS, "email": "Ada@Example.com"}

    response = await finalize_login(db_session, provider="google", claims=claims, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == existing.id
    assert len(await _identities(db_session, "google-sub-123")) == 1


@pytest.mark.integration
@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.INVITED])
async def test_finalize_login_activates_an_onboarding_account_it_links(
    db_session: AsyncSession,
    member_role: Role,
    status: UserStatus,
) -> None:
    """A provider-verified email is at least as strong as our own verification loop.

    The invite-time roles survive — linking activates the account, it does not
    re-provision it.
    """
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=status)
    if status is UserStatus.INVITED:
        await _make_invitation(db_session, existing.id)

    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.id == existing.id
    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None
    assert [r.id for r in user.roles] == [member_role.id]


@pytest.mark.integration
async def test_finalize_login_activates_an_invited_account_with_no_platform_invitation(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """`user.invitations` is the platform-scoped relationship only — a group-invited
    account (`invite_to_group`) has no platform invitation at all, not a revoked or
    expired one, and must not be refused on that basis: there was never anything to
    revoke, and no accept link exists to ever unstick it if it were refused.
    """
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INVITED)

    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.id == existing.id
    assert user.status is UserStatus.ACTIVE


@pytest.mark.integration
@pytest.mark.usefixtures("celery_enqueue_stub")
async def test_finalize_login_clears_a_self_registered_password_on_activation(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """A `pending` row may carry a password an attacker planted via self-registration
    before the real owner ever touched the account — activating via Google must not
    leave it live and usable for password login. The notification says so, since
    `request_password_reset` doubling as "set a password" is the only way back.
    """
    existing = await create_user(
        db_session,
        email="ada@example.com",
        roles=[member_role],
        status=UserStatus.PENDING,
        password=SecretStr("attacker-planted-password"),
    )

    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.id == existing.id
    assert user.status is UserStatus.ACTIVE
    assert user.password is None
    assert user.password_cleared_at is not None
    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.context["password_cleared"] is True


@pytest.mark.integration
@pytest.mark.usefixtures("celery_enqueue_stub")
@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.INVITED])
async def test_finalize_login_notifies_the_user_on_activation(
    db_session: AsyncSession,
    member_role: Role,
    status: UserStatus,
) -> None:
    """Same notification as the password-verify path — the account holder otherwise
    has no signal that a Google login just changed their account's state.
    """
    user = await create_user(db_session, email="ada@example.com", roles=[member_role], first_name="Ada", status=status)
    if status is UserStatus.INVITED:
        await _make_invitation(db_session, user.id)

    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.context["user_name"] == "Ada"
    # Neither PENDING nor INVITED here ever had a password to clear.
    assert activated.context["password_cleared"] is False
    activated_user = await _user_by_email(db_session, "ada@example.com")
    assert activated_user.password_cleared_at is None


@pytest.mark.integration
async def test_finalize_login_refuses_a_deactivated_account(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INACTIVE)

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_refuses_an_invited_account_whose_invitation_was_revoked(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """Stricter than `register_user`'s `INVITED` branch (which reissues rather than
    blocks on an expired invite) — an admin who revokes a bad invite must not have
    it undone by the invitee completing it via Google instead.
    """
    user = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INVITED)
    await _make_invitation(db_session, user.id, status=InvitationStatus.REVOKED)

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    refreshed = await _user_by_email(db_session, "ada@example.com")
    assert refreshed.status is UserStatus.INVITED
    assert await _identities(db_session, "google-sub-123") == []


@pytest.mark.integration
async def test_finalize_login_refuses_an_invited_account_whose_invitation_expired(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    user = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INVITED)
    await _make_invitation(
        db_session,
        user.id,
        status=InvitationStatus.PENDING,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    assert await _identities(db_session, "google-sub-123") == []


@pytest.mark.integration
async def test_finalize_login_refuses_an_identity_whose_account_was_removed(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """The bare `User.soft_delete()` model method (not the cascading `soft_delete_user`
    service helper below) leaves identity rows behind — resolving one must still treat
    the now-gone account as refused rather than resurrecting it.
    """
    user = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.ACTIVE)
    db_session.add(ProviderIdentity(provider="google", subject="google-sub-123", user_id=user.id))
    await db_session.flush()
    user.soft_delete(None)
    db_session.add(user)
    await db_session.flush()

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_recovers_after_the_linked_account_is_soft_deleted_and_recreated(
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """`soft_delete_user` cascades to `provider_identities` — recreating the same email
    after a deletion must let a fresh Google login land on the new account instead of
    resolving the tombstoned identity forever.
    """
    old = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.ACTIVE)
    db_session.add(ProviderIdentity(provider="google", subject="google-sub-123", user_id=old.id))
    await db_session.flush()
    await soft_delete_user(db_session, old, by_id=uuid4())

    new = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.ACTIVE)

    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == new.id
    identities = await _identities(db_session, "google-sub-123")
    assert [i.user_id for i in identities] == [new.id]


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_refuses_a_linked_account_deactivated_after_linking(db_session: AsyncSession) -> None:
    """The never-linked and soft-deleted cases are covered above; an admin can also just flip
    an already-linked account to `inactive` without touching the row — the existing-identity
    lookup path must refuse that too.
    """
    await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())
    user = await _user_by_email(db_session, "ada@example.com")
    user.status = UserStatus.INACTIVE
    db_session.add(user)
    await db_session.flush()

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_recovers_from_a_concurrent_signup_for_the_same_email(
    db_session: AsyncSession,
    member_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two first-time Google logins racing on the same new email: whichever loses the
    `create_user` insert must adopt the winner's row instead of surfacing `ConflictError`.

    Forces the real race window by making the *first* `get_user_by_email` check miss (as it
    would for both racers before either has committed) against a row that genuinely already
    exists — so `create_user` hits the real partial-unique-email index, not a mocked error,
    proving the savepoint-and-adopt recovery in `_resolve_user` rather than trusting it by
    inspection.
    """
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.ACTIVE)
    real_get_user_by_email = oidc_module.get_user_by_email
    calls = 0

    async def flaky_get_user_by_email(session: AsyncSession, email: str) -> User | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_get_user_by_email(session, email)

    monkeypatch.setattr(oidc_module, "get_user_by_email", flaky_get_user_by_email)

    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == existing.id
    identities = await _identities(db_session, "google-sub-123")
    assert [i.user_id for i in identities] == [existing.id]


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.INVITED])
async def test_finalize_login_activates_an_onboarding_account_recovered_from_a_signup_race(
    db_session: AsyncSession,
    member_role: Role,
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
) -> None:
    """Same activate-on-link invariant as `test_finalize_login_activates_an_onboarding_account_it_links`,
    forced through the `ConflictError` race-recovery branch instead of the direct-resolution
    path: the activate/refuse check must run on both, or an onboarding account that loses
    the signup race is left permanently unactivated instead of linked and promoted.
    """
    existing = await create_user(db_session, email="ada@example.com", roles=[member_role], status=status)
    if status is UserStatus.INVITED:
        await _make_invitation(db_session, existing.id)
    real_get_user_by_email = oidc_module.get_user_by_email
    calls = 0

    async def flaky_get_user_by_email(session: AsyncSession, email: str) -> User | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_get_user_by_email(session, email)

    monkeypatch.setattr(oidc_module, "get_user_by_email", flaky_get_user_by_email)

    response = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    session = decode_session_jwt(response.access_token, secret=SECRET, algorithm="HS256")
    assert session.id == existing.id
    user = await _user_by_email(db_session, "ada@example.com")
    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None
    identities = await _identities(db_session, "google-sub-123")
    assert [i.user_id for i in identities] == [existing.id]


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_refuses_a_deactivated_account_recovered_from_a_signup_race(
    db_session: AsyncSession,
    member_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same refuse-before-link invariant as `test_finalize_login_refuses_a_deactivated_account`,
    forced through the `ConflictError` race-recovery branch: that branch must not link an
    identity to an account it's about to refuse.
    """
    await create_user(db_session, email="ada@example.com", roles=[member_role], status=UserStatus.INACTIVE)
    real_get_user_by_email = oidc_module.get_user_by_email
    calls = 0

    async def flaky_get_user_by_email(session: AsyncSession, email: str) -> User | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_get_user_by_email(session, email)

    monkeypatch.setattr(oidc_module, "get_user_by_email", flaky_get_user_by_email)

    with pytest.raises(InactiveAccountError):
        await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    assert await _identities(db_session, "google-sub-123") == []


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_recovers_from_a_concurrent_identity_insert(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two logins racing on the same new `(provider, sub)`: whichever loses the
    `ProviderIdentity` insert must adopt the winner's row instead of failing the login.

    The first login runs for real and creates the row; the second's identity lookup is
    forced to miss once, so its INSERT hits the real unique index the first login's row
    already occupies — the same shape as the email race above, exercising the
    adopt-on-`IntegrityError` branch against a genuine constraint violation.
    """
    first = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())
    winner_id = decode_session_jwt(first.access_token, secret=SECRET, algorithm="HS256").id

    real_get_identity = oidc_module._get_identity
    calls = 0

    async def flaky_get_identity(session: AsyncSession, *, provider: str, subject: str) -> ProviderIdentity | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_get_identity(session, provider=provider, subject=subject)

    monkeypatch.setattr(oidc_module, "_get_identity", flaky_get_identity)

    second = await finalize_login(db_session, provider="google", claims=CLAIMS, settings=_settings())

    assert decode_session_jwt(second.access_token, secret=SECRET, algorithm="HS256").id == winner_id
    assert len(await _identities(db_session, "google-sub-123")) == 1


@pytest.mark.integration
async def test_finalize_login_rejects_claims_without_sub(db_session: AsyncSession) -> None:
    with pytest.raises(MissingClaimError):
        await finalize_login(db_session, provider="google", claims={"email": "a@example.com"}, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_rejects_claims_without_email(db_session: AsyncSession) -> None:
    with pytest.raises(MissingClaimError):
        await finalize_login(db_session, provider="google", claims={"sub": "abc"}, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_rejects_a_non_string_email_claim(db_session: AsyncSession) -> None:
    """A malformed claim (not a bare-absent one) must not crash `.strip()` uncaught."""
    with pytest.raises(MissingClaimError):
        await finalize_login(db_session, provider="google", claims={"sub": "abc", "email": 12345}, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_rejects_a_non_string_sub_claim(db_session: AsyncSession) -> None:
    """A non-string `sub` reaching `_get_identity`'s query against a varchar column is a
    raw DB error, not a `MissingClaimError` — none of the callback's exception handlers
    catch that, so it must be rejected here before it ever reaches the query.
    """
    with pytest.raises(MissingClaimError):
        await finalize_login(
            db_session, provider="google", claims={"sub": 12345, "email": "ada@example.com"}, settings=_settings()
        )


@pytest.mark.integration
async def test_finalize_login_rejects_a_whitespace_only_email_claim(db_session: AsyncSession) -> None:
    """`\"   \"` is a truthy, non-empty `str` — the absent-claim guard alone lets it
    through; it must still be rejected once it strips down to `""`, or it persists as a
    real ACTIVE account with a blank email that a second malformed claim can then find
    and link a second identity to.
    """
    with pytest.raises(MissingClaimError):
        await finalize_login(db_session, provider="google", claims={"sub": "abc", "email": "   "}, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_drops_a_non_string_name_claim_instead_of_crashing(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """`User` runs no validation on construction — a non-string `given_name`/`family_name`
    reaching `create_user` as-is raises an uncaught DB error on flush, same failure shape
    as the unguarded `sub`/`email` claims above. First-ever-login provisioning must still
    succeed, just without the malformed name.
    """
    claims = {
        "sub": "abc",
        "email": "ada@example.com",
        "email_verified": True,
        "given_name": 12345,
        "family_name": ["a", "b"],
    }

    await finalize_login(db_session, provider="google", claims=claims, settings=_settings())

    user = await _user_by_email(db_session, "ada@example.com")
    assert user.first_name is None
    assert user.last_name is None


@pytest.mark.integration
async def test_finalize_login_treats_the_string_false_email_verified_claim_as_unverified(
    db_session: AsyncSession,
) -> None:
    """Some IdPs (Cognito, ADFS, Okta) report this claim as a JSON string, not a bare bool —
    a bare `bool("false")` is `True`, which would wrongly trust an unverified email.
    """
    claims = {"sub": "abc", "email": "ada@example.com", "email_verified": "false", "name": "Ada"}

    with pytest.raises(UnverifiedEmailError):
        await finalize_login(db_session, provider="google", claims=claims, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_rejects_unverified_email_for_untrusted_provider(db_session: AsyncSession) -> None:
    # Google's spec has trust_email_unverified=False (the default).
    claims = {
        "sub": "abc",
        "email": "spoofed@example.com",
        "email_verified": False,
        "name": "Mallory",
    }

    with pytest.raises(UnverifiedEmailError):
        await finalize_login(db_session, provider="google", claims=claims, settings=_settings())


@pytest.mark.integration
async def test_finalize_login_treats_missing_email_verified_as_unverified(db_session: AsyncSession) -> None:
    claims = {"sub": "abc", "email": "ada@example.com", "name": "Ada"}

    with pytest.raises(UnverifiedEmailError):
        await finalize_login(db_session, provider="google", claims=claims, settings=_settings())


@pytest.mark.integration
@pytest.mark.usefixtures("default_role")
async def test_finalize_login_does_not_provision_when_claims_are_rejected(db_session: AsyncSession) -> None:
    """Guard the ordering: validation runs before anything is written."""
    with pytest.raises(UnverifiedEmailError):
        await finalize_login(
            db_session,
            provider="google",
            claims={"sub": "google-sub-123", "email": "ada@example.com"},
            settings=_settings(),
        )

    assert await _identities(db_session, "google-sub-123") == []
