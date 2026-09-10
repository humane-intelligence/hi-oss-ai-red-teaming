"""Integration tests for `/v1/auth/password-resets` router."""

from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import pytest_asyncio
import time_machine
from fastapi import status
from httpx import ASGITransport
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import PasswordResetToken
from app.core.auth.models import PasswordResetTokenStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services import password_resets as password_resets_service
from app.core.auth.services import session_revocation
from app.core.auth.services.password_resets import hash_token
from app.core.auth.services.password_resets import issue_password_reset
from app.core.auth.services.password_resets import request_password_reset
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.users import create_user
from app.core.config import Settings
from app.core.database import get_db
from app.core.email import send_email
from app.core.email.models import OutboundEmail
from app.core.platform_settings.service import update_platform_settings
from app.main import app as fastapi_app
from tests.api.v1.conftest import bearer

_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def enqueue_stub(celery_enqueue_stub: MagicMock) -> MagicMock:
    """Module-wide: every path here dispatches mail. Aliases the shared spy under this module's name."""
    return celery_enqueue_stub


@pytest.fixture
async def anon_client(
    _configured_settings: Settings,
    db_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    fastapi_app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=fastapi_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def member_role(db_session: AsyncSession) -> Role:
    role = Role(name="member", description="Member", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def active_user(db_session: AsyncSession, member_role: Role) -> User:
    return await create_user(
        db_session,
        email="ada@example.com",
        first_name="Ada",
        last_name="Lovelace",
        password=SecretStr(_PASSWORD),
        status=UserStatus.ACTIVE,
        email_verified=True,
        roles=[member_role],
    )


@pytest_asyncio.fixture
async def other_active_user(db_session: AsyncSession, member_role: Role) -> User:
    return await create_user(
        db_session,
        email="grace@example.com",
        first_name="Grace",
        last_name="Hopper",
        password=SecretStr(_PASSWORD),
        status=UserStatus.ACTIVE,
        email_verified=True,
        roles=[member_role],
    )


async def _clear_revocation(user_id: UUID) -> None:
    async with session_revocation._redis() as client:
        await client.delete(session_revocation._key(user_id))


async def _outbound_count(session: AsyncSession) -> int:
    result = await session.execute(select(OutboundEmail))
    return len(result.scalars().all())


async def _backdate_resets(session: AsyncSession, user_id: UUID, *, seconds: int) -> None:
    """Age this user's reset tokens by `seconds` on the database clock."""
    await session.execute(
        update(PasswordResetToken)
        .where(col(PasswordResetToken.user_id) == user_id)
        .values(created_at=func.now() - timedelta(seconds=seconds)),
    )


async def _live_reset_for(session: AsyncSession, user_id) -> PasswordResetToken:
    result = await session.execute(
        PasswordResetToken.live_select().where(col(PasswordResetToken.user_id) == user_id),
    )
    return result.scalars().one()


# ---------------------------------------------------------------------------
# POST /api/v1/auth/password-resets/request
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_request_issues_pending_token_and_queues_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        response = await anon_client.post(
            "/api/v1/auth/password-resets/request",
            json={"email": active_user.email},
        )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    reset = await _live_reset_for(db_session, active_user.id)
    assert reset.status is PasswordResetTokenStatus.PENDING
    # Default password_reset_ttl_hours = 24
    assert reset.expires_at == now + timedelta(hours=24)
    assert await _outbound_count(db_session) == 1


@pytest.mark.integration
async def test_request_normalises_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "Ada@Example.COM"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await _live_reset_for(db_session, active_user.id)


@pytest.mark.integration
async def test_request_for_unknown_email_returns_204_and_no_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "ghost@example.com"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 0
    result = await db_session.execute(PasswordResetToken.live_select())
    assert result.scalars().all() == []


@pytest.mark.integration
async def test_request_for_inactive_user_returns_204_and_no_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    await create_user(
        db_session,
        email="frozen@example.com",
        password=SecretStr(_PASSWORD),
        status=UserStatus.INACTIVE,
        email_verified=True,
        roles=[member_role],
    )

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "frozen@example.com"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 0


@pytest.mark.integration
async def test_request_for_a_cleared_password_account_doubles_as_set_a_password(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """Recovering from an OIDC-triggered clear (`password_cleared_at` set) has nothing
    to *reset*, but `confirm_password_reset` already handles minting a first password
    generically — the self-service request must not silently no-op here, or there is
    no way back once Google login clears the password an attacker planted (or a
    legitimate self-registrant loses on their own).
    """
    user = await create_user(
        db_session,
        email="oidc@example.com",
        status=UserStatus.ACTIVE,
        email_verified=True,
        roles=[member_role],
    )
    user.password_cleared_at = datetime.now(UTC)
    db_session.add(user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "oidc@example.com"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    reset = await _live_reset_for(db_session, user.id)
    assert reset.status is PasswordResetTokenStatus.PENDING
    assert await _outbound_count(db_session) == 1


@pytest.mark.integration
async def test_request_for_a_pure_idp_account_returns_204_and_no_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    member_role: Role,
) -> None:
    """An account whose *first-ever* login was via Google never had a local password
    to lose — unlike the cleared-password case above, there is no local-login door to
    reopen for it, and self-service must not open one: password login never re-checks
    the IdP link, so a self-minted password here would bypass whatever the org relies
    on Google for, permanently.
    """
    await create_user(
        db_session,
        email="pure-idp@example.com",
        status=UserStatus.ACTIVE,
        email_verified=True,
        roles=[member_role],
    )

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "pure-idp@example.com"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 0


@pytest.mark.integration
async def test_request_revokes_previous_pending_tokens(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    # Cooldown off: this pins the re-issue behaviour, which the throttle would otherwise mask.
    await update_platform_settings(db_session, password_reset_cooldown_seconds=0)
    await request_password_reset(db_session, email=active_user.email)
    await db_session.commit()
    previous = await _live_reset_for(db_session, active_user.id)
    previous_id = previous.id

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await db_session.refresh(previous)
    assert previous.status is PasswordResetTokenStatus.REVOKED
    assert previous.revoked_at is not None
    rows = (
        (
            await db_session.execute(
                PasswordResetToken.live_select()
                .where(col(PasswordResetToken.user_id) == active_user.id)
                .where(col(PasswordResetToken.status) == PasswordResetTokenStatus.PENDING),
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id != previous_id


@pytest.mark.integration
async def test_request_inside_the_cooldown_sends_nothing_and_keeps_the_live_link(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    """A throttled request is indistinguishable from a served one — and must not revoke.

    Re-issuing invalidates the previous link, so a throttle that ran *after* the revoke would
    turn repeat clicking into a way to lock the account out of its own reset mail.
    """
    await request_password_reset(db_session, email=active_user.email)
    issued = await _live_reset_for(db_session, active_user.id)

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 1
    still_live = await _live_reset_for(db_session, active_user.id)
    assert still_live.id == issued.id
    assert still_live.status is PasswordResetTokenStatus.PENDING


@pytest.mark.integration
async def test_request_after_the_cooldown_issues_again(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    await request_password_reset(db_session, email=active_user.email)
    # The window is measured on the database clock (`created_at` is server-stamped), so ageing
    # the row is the honest way to move time here — travelling the app clock alone would not.
    await _backdate_resets(db_session, active_user.id, seconds=61)  # default cooldown is 60s

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 2


@pytest.mark.integration
async def test_request_stops_at_the_daily_cap_even_past_the_cooldown(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    await update_platform_settings(db_session, password_reset_cooldown_seconds=0, password_reset_max_per_day=2)

    for _ in range(3):
        response = await anon_client.post(
            "/api/v1/auth/password-resets/request",
            json={"email": active_user.email},
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    assert await _outbound_count(db_session) == 2


@pytest.mark.integration
async def test_the_throttle_windows_are_per_account_not_platform_wide(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    other_active_user: User,
) -> None:
    """Exhausting one account's window must leave another account's untouched.

    Every other throttle test drives a single account, so dropping the `user_id` predicate from
    the window query would turn both limits global with nothing failing — and one caller's
    requests would then disable "forgot password" for every account on the platform.
    """
    await update_platform_settings(db_session, password_reset_cooldown_seconds=0, password_reset_max_per_day=1)

    for _ in range(2):
        exhausting = await anon_client.post(
            "/api/v1/auth/password-resets/request",
            json={"email": active_user.email},
        )
        assert exhausting.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 1

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": other_active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 2


@pytest.mark.integration
async def test_daily_cap_counts_a_rolling_window_not_a_calendar_day(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    await update_platform_settings(db_session, password_reset_cooldown_seconds=0, password_reset_max_per_day=1)
    await request_password_reset(db_session, email=active_user.email)
    await _backdate_resets(db_session, active_user.id, seconds=25 * 3600)

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 2


@pytest.mark.integration
async def test_zero_cooldown_disables_the_gate(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    await update_platform_settings(db_session, password_reset_cooldown_seconds=0)
    await request_password_reset(db_session, email=active_user.email)

    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await _outbound_count(db_session) == 2


@pytest.mark.integration
async def test_throttling_does_not_apply_to_an_unknown_email(
    anon_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # The gate reads the account's tokens, so an address with no account must not reach it: a
    # crash there would be the enumeration signal the always-204 contract exists to avoid. Only
    # the outcome is pinned here — response times are not compared.
    for _ in range(3):
        response = await anon_client.post(
            "/api/v1/auth/password-resets/request",
            json={"email": "ghost@example.com"},
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    assert await _outbound_count(db_session) == 0


@pytest.mark.integration
async def test_account_deactivated_between_the_lookup_and_the_lock_sends_nothing(
    db_session: AsyncSession,
    active_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked re-read decides, not the unlocked one that got us here.

    The lock exists so the throttle can't be raced; that only holds if the state it re-reads is
    the state under the lock — hence the second eligibility check, which this pins.
    """
    real_get_user = password_resets_service.get_user

    async def deactivate_then_load(session: AsyncSession, user_id: UUID, *, for_update: bool = False) -> User:
        user = await real_get_user(session, user_id, for_update=for_update)
        user.status = UserStatus.INACTIVE
        return user

    monkeypatch.setattr(password_resets_service, "get_user", deactivate_then_load)

    await request_password_reset(db_session, email=active_user.email)

    assert await _outbound_count(db_session) == 0
    result = await db_session.execute(PasswordResetToken.live_select())
    assert result.scalars().all() == []


@pytest.mark.integration
async def test_an_admin_send_fills_the_window_the_next_self_service_request_measures(
    db_session: AsyncSession,
    active_user: User,
) -> None:
    # The contract says so in the admin route's own description: same inbox, same flood.
    spec = await issue_password_reset(db_session, active_user, triggered_by_admin=True)
    await send_email(db_session, spec.template, spec.to, spec.context, secret_context=spec.secret_context)

    await request_password_reset(db_session, email=active_user.email)

    assert await _outbound_count(db_session) == 1


@pytest.mark.integration
async def test_request_rejects_invalid_email_returns_422(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": "not-an-email"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# ---------------------------------------------------------------------------
# POST /api/v1/auth/password-resets/confirm
# ---------------------------------------------------------------------------


async def _issue_token(session: AsyncSession, enqueue: MagicMock, user: User) -> str:
    """Issue a reset token and extract the raw value from the enqueued task signature.

    The token rides `secret_context`, not the persisted `outbound_emails.context`.
    """
    await request_password_reset(session, email=user.email)
    secret = enqueue.call_args.kwargs["args"][1]
    return secret["reset_url"].split("token=", 1)[1]


@pytest.mark.integration
async def test_request_builds_reset_url_for_frontend_route(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
    active_user: User,
) -> None:
    """The reset link must target the SPA's `/password-reset/confirm` route."""
    await request_password_reset(db_session, email=active_user.email)

    reset_url = enqueue_stub.call_args.kwargs["args"][1]["reset_url"]
    assert "/password-reset/confirm?token=" in reset_url


@pytest.mark.integration
async def test_confirm_changes_password_and_marks_token_used(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    await db_session.commit()
    old_hash = active_user.password

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await db_session.refresh(active_user)
    assert active_user.password != old_hash
    assert verify_password("brand-new-password-12345", active_user.password or "") is True
    reset = await _live_reset_for(db_session, active_user.id)
    assert reset.status is PasswordResetTokenStatus.USED
    assert reset.used_at is not None


@pytest.mark.integration
async def test_confirm_sets_a_first_password_for_a_previously_passwordless_user(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    member_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """`confirm_password_reset` doesn't distinguish "reset" from "set a first one" —
    both are just `user.password = hash_password(...)`. Proves the whole round trip
    for the IdP-only-account case, not just that a token gets issued.
    """
    user = await create_user(
        db_session, email="oidc@example.com", status=UserStatus.ACTIVE, email_verified=True, roles=[member_role]
    )
    # Satisfies `request_password_reset`'s gate only, so `_issue_token` below actually
    # issues one — `confirm_password_reset` itself never reads this field.
    user.password_cleared_at = datetime.now(UTC)
    db_session.add(user)
    await db_session.commit()
    raw_token = await _issue_token(db_session, enqueue_stub, user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await db_session.refresh(user)
    assert verify_password("brand-new-password-12345", user.password or "") is True


@pytest.mark.integration
async def test_confirm_revokes_the_users_live_sessions(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    """A reset may be the answer to stolen credentials, so the thief's token must die with it."""
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    await db_session.commit()
    held_before_reset = bearer(active_user)

    try:
        response = await anon_client.post(
            "/api/v1/auth/password-resets/confirm",
            json={"token": raw_token, "password": "brand-new-password-12345"},
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        after = await anon_client.get("/api/v1/auth/me", headers=held_before_reset)
        assert after.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        await _clear_revocation(active_user.id)


@pytest.mark.integration
async def test_confirm_honours_the_configured_character_classes(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    await update_platform_settings(db_session, password_require_digit=True)
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "uncommon-passphrase"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_missing_digit"


@pytest.mark.integration
async def test_confirm_common_password_returns_400(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "password1234"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == "application/problem+json"
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_too_common"


@pytest.mark.integration
async def test_confirm_unknown_token_returns_404(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": "does-not-exist", "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.integration
async def test_confirm_used_token_returns_410(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    await db_session.commit()
    first = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )
    assert first.status_code == status.HTTP_204_NO_CONTENT

    replay = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "another-new-password-12345"},
    )

    assert replay.status_code == status.HTTP_410_GONE
    assert "used" in replay.json()["detail"]


@pytest.mark.integration
async def test_confirm_revoked_token_returns_410(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    reset = await _live_reset_for(db_session, active_user.id)
    reset.status = PasswordResetTokenStatus.REVOKED
    reset.revoked_at = datetime.now(UTC)
    db_session.add(reset)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_410_GONE
    assert "revoked" in response.json()["detail"]


@pytest.mark.integration
async def test_confirm_expired_token_returns_410_without_status_write(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    reset = await _live_reset_for(db_session, active_user.id)
    reset.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.add(reset)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_410_GONE
    assert "expired" in response.json()["detail"]
    # Expiry is read-only: status stays PENDING on disk.
    await db_session.refresh(reset)
    assert reset.status is PasswordResetTokenStatus.PENDING


@pytest.mark.integration
async def test_confirm_short_password_returns_422(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": "anything", "password": "short"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_confirm_for_deactivated_user_returns_410(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    active_user.status = UserStatus.INACTIVE
    db_session.add(active_user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_410_GONE
    assert "no longer available" in response.json()["detail"]


@pytest.mark.integration
async def test_confirm_for_soft_deleted_user_returns_410(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    active_user.soft_delete(None)
    db_session.add(active_user)
    await db_session.commit()

    response = await anon_client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": raw_token, "password": "brand-new-password-12345"},
    )

    assert response.status_code == status.HTTP_410_GONE


@pytest.mark.integration
async def test_request_with_email_envelope_does_not_leak_token_in_response(
    anon_client: AsyncClient,
    active_user: User,
) -> None:
    response = await anon_client.post(
        "/api/v1/auth/password-resets/request",
        json={"email": active_user.email},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""


@pytest.mark.integration
async def test_token_hash_stored_on_disk_is_not_the_raw_token(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    enqueue_stub: MagicMock,
) -> None:
    raw_token = await _issue_token(db_session, enqueue_stub, active_user)
    reset = await _live_reset_for(db_session, active_user.id)

    assert reset.token_hash != raw_token
    assert reset.token_hash == hash_token(raw_token)
    assert len(reset.token_hash) == 64  # sha256 hex
