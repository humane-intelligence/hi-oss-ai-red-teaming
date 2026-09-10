"""Integration tests for `app.core.auth.services.account`."""

import time
from datetime import UTC
from datetime import datetime

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.password_policy import MAX_PASSWORD_LENGTH
from app.core.auth.password_policy import PasswordPolicyError
from app.core.auth.services import account as account_module
from app.core.auth.services.account import CurrentPasswordError
from app.core.auth.services.account import change_own_password
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.session_revocation import is_revoked
from app.core.auth.services.users import create_user
from app.core.exceptions import ConflictError
from tests.conftest import ACTIVE_USER_PASSWORD

_NEW = SecretStr("brand-new-pass-456")


@pytest.mark.integration
async def test_change_own_password_rotates_the_hash_and_revokes_sessions(
    db_session: AsyncSession, active_user: User
) -> None:
    before = int(time.time())

    await change_own_password(
        db_session, active_user, current_password=SecretStr(ACTIVE_USER_PASSWORD), new_password=_NEW
    )

    assert verify_password(_NEW.get_secret_value(), active_user.password or "")
    assert not verify_password(ACTIVE_USER_PASSWORD, active_user.password or "")
    assert await is_revoked(active_user.id, before)


@pytest.mark.integration
async def test_change_own_password_rejects_a_wrong_current_password(
    db_session: AsyncSession, active_user: User
) -> None:
    with pytest.raises(CurrentPasswordError):
        await change_own_password(
            db_session, active_user, current_password=SecretStr("not-the-password"), new_password=_NEW
        )

    assert verify_password(ACTIVE_USER_PASSWORD, active_user.password or "")


@pytest.mark.integration
async def test_change_own_password_caps_the_current_password_before_hashing(
    db_session: AsyncSession, active_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ordering is the point of the guard: an over-cap input must never reach argon2.
    # `CurrentPasswordError` alone would also come back from a plain verification miss.
    def fail_if_called(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("verify_password ran on an over-cap input")

    monkeypatch.setattr(account_module, "verify_password", fail_if_called)
    oversized = SecretStr("x" * (MAX_PASSWORD_LENGTH + 1))

    with pytest.raises(CurrentPasswordError):
        await change_own_password(db_session, active_user, current_password=oversized, new_password=_NEW)


@pytest.mark.integration
@pytest.mark.parametrize("cleared", [False, True], ids=["pure_idp", "oidc_cleared"])
async def test_change_own_password_refuses_a_passwordless_account(
    db_session: AsyncSession, member_role: Role, cleared: bool
) -> None:
    user = await create_user(
        db_session,
        email=f"idp-{'cleared' if cleared else 'pure'}@example.com",
        roles=[member_role],
        status=UserStatus.ACTIVE,
    )
    if cleared:
        user.password_cleared_at = datetime.now(UTC)
        db_session.add(user)
        await db_session.flush()

    with pytest.raises(ConflictError):
        await change_own_password(db_session, user, current_password=SecretStr("whatever-123"), new_password=_NEW)


@pytest.mark.integration
async def test_change_own_password_runs_the_password_policy(db_session: AsyncSession, active_user: User) -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        await change_own_password(
            db_session,
            active_user,
            current_password=SecretStr(ACTIVE_USER_PASSWORD),
            new_password=SecretStr("password1234"),
        )

    errors = exc_info.value.errors
    assert errors is not None
    assert errors[0].type == "password_too_common"
    assert verify_password(ACTIVE_USER_PASSWORD, active_user.password or "")


@pytest.mark.integration
async def test_change_own_password_to_the_same_value_succeeds_and_still_revokes(
    db_session: AsyncSession, active_user: User
) -> None:
    # Deliberately no reuse check; the session rotation is the point.
    before = int(time.time())

    await change_own_password(
        db_session,
        active_user,
        current_password=SecretStr(ACTIVE_USER_PASSWORD),
        new_password=SecretStr(ACTIVE_USER_PASSWORD),
    )

    assert await is_revoked(active_user.id, before)
