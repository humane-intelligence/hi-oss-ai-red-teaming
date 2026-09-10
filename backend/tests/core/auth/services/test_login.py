"""Integration tests for `app.core.auth.services.login`."""

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.login import authenticate_credentials
from app.core.auth.services.users import create_user
from app.core.exceptions import UnauthorizedError
from tests.conftest import ACTIVE_USER_PASSWORD as _PASSWORD


@pytest.mark.integration
async def test_authenticate_credentials_returns_user_on_match(db_session: AsyncSession, active_user: User) -> None:
    result = await authenticate_credentials(db_session, email=active_user.email, password=_PASSWORD)

    assert result.id == active_user.id


@pytest.mark.integration
async def test_authenticate_credentials_eager_loads_roles(db_session: AsyncSession, active_user: User) -> None:
    result = await authenticate_credentials(db_session, email=active_user.email, password=_PASSWORD)

    assert "roles" in result.__dict__
    assert [role.name for role in result.roles] == ["member"]


@pytest.mark.integration
async def test_authenticate_credentials_rejects_wrong_password(db_session: AsyncSession, active_user: User) -> None:
    with pytest.raises(UnauthorizedError, match=r"Invalid email or password\."):
        await authenticate_credentials(db_session, email=active_user.email, password="wrong")


@pytest.mark.integration
async def test_authenticate_credentials_rejects_unknown_email(db_session: AsyncSession) -> None:
    with pytest.raises(UnauthorizedError, match=r"Invalid email or password\."):
        await authenticate_credentials(db_session, email="ghost@example.com", password=_PASSWORD)


@pytest.mark.integration
async def test_authenticate_credentials_rejects_passwordless_account(
    db_session: AsyncSession, member_role: Role
) -> None:
    # IdP-only account: status ACTIVE, but no password set.
    user = await create_user(
        db_session,
        email="oidc-only@example.com",
        status=UserStatus.ACTIVE,
        roles=[member_role],
    )
    assert user.password is None

    with pytest.raises(UnauthorizedError, match=r"Invalid email or password\."):
        await authenticate_credentials(db_session, email=user.email, password=_PASSWORD)


@pytest.mark.integration
@pytest.mark.parametrize("status", [UserStatus.INVITED, UserStatus.PENDING, UserStatus.INACTIVE])
async def test_authenticate_credentials_rejects_non_active_status(
    db_session: AsyncSession, member_role: Role, status: UserStatus
) -> None:
    user = await create_user(
        db_session,
        email=f"{status.value}@example.com",
        password=SecretStr(_PASSWORD),
        status=status,
        roles=[member_role],
    )

    with pytest.raises(UnauthorizedError, match=r"Invalid email or password\."):
        await authenticate_credentials(db_session, email=user.email, password=_PASSWORD)
