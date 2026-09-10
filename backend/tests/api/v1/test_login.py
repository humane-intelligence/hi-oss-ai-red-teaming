"""Integration tests for `POST /api/v1/auth/login`."""

from collections.abc import AsyncIterator

import pytest
from fastapi import status
from httpx import ASGITransport
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import Settings
from app.core.database import get_db
from app.main import app as fastapi_app
from tests.api.v1.conftest import SECRET
from tests.conftest import ACTIVE_USER_PASSWORD as PASSWORD

PROBLEM_CT = "application/problem+json"


@pytest.fixture
async def login_client(
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


@pytest.mark.integration
async def test_login_returns_token_pair_with_local_provider(login_client: AsyncClient, active_user: User) -> None:
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": PASSWORD},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["expires_in"] == 3600
    assert body["access_token"]
    assert body["refresh_token"]

    session_user = decode_session_jwt(body["access_token"], secret=SECRET, algorithm="HS256")
    assert session_user.id == active_user.id
    assert session_user.email == active_user.email
    assert session_user.provider == "local"
    assert session_user.email_verified is True


@pytest.mark.integration
async def test_login_normalises_email_casing(login_client: AsyncClient, active_user: User) -> None:
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": "  ADA@Example.COM  ", "password": PASSWORD},
    )

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_login_rejects_wrong_password(login_client: AsyncClient, active_user: User) -> None:
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "wrong-password"},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
async def test_login_accepts_short_password_attempt_without_422(login_client: AsyncClient, active_user: User) -> None:
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "short"},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_login_oversized_password_collapses_to_401(login_client: AsyncClient, active_user: User) -> None:
    # Login applies no length policy: an over-cap password is rejected before the
    # argon2 verify (anti-DoS) but surfaces as the same uniform 401, not a 422 that
    # would leak the policy bound.
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "x" * 129},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
async def test_login_empty_password_collapses_to_401(login_client: AsyncClient, active_user: User) -> None:
    # An empty password is a wrong credential, not a malformed request — uniform 401.
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": ""},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
async def test_login_rejects_unknown_email(login_client: AsyncClient) -> None:
    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": PASSWORD},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
async def test_login_rejects_passwordless_account(
    login_client: AsyncClient, db_session: AsyncSession, member_role: Role
) -> None:
    await create_user_service(
        db_session,
        email="oidc-only@example.com",
        status=UserStatus.ACTIVE,
        roles=[member_role],
    )

    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": "oidc-only@example.com", "password": PASSWORD},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
@pytest.mark.parametrize("user_status", [UserStatus.INVITED, UserStatus.PENDING, UserStatus.INACTIVE])
async def test_login_rejects_non_active_status(
    login_client: AsyncClient,
    db_session: AsyncSession,
    member_role: Role,
    user_status: UserStatus,
) -> None:
    await create_user_service(
        db_session,
        email=f"{user_status.value}@example.com",
        password=SecretStr(PASSWORD),
        status=user_status,
        roles=[member_role],
    )

    response = await login_client.post(
        "/api/v1/auth/login",
        json={"email": f"{user_status.value}@example.com", "password": PASSWORD},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid email or password."


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        {"email": "ada@example.com"},
        {"password": PASSWORD},
        {"email": "not-an-email", "password": PASSWORD},
        {},
    ],
)
async def test_login_rejects_invalid_payload(login_client: AsyncClient, payload: dict[str, str]) -> None:
    response = await login_client.post("/api/v1/auth/login", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
