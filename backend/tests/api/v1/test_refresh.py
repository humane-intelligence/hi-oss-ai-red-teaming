"""Integration tests for `POST /api/v1/auth/refresh`."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import status
from httpx import ASGITransport
from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services import session_revocation
from app.core.auth.services.jwt import decode_refresh_jwt
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.jwt import encode_refresh_jwt
from app.core.auth.services.session_revocation import revoke_user_sessions
from app.core.config import Settings
from app.core.database import get_db
from app.main import app as fastapi_app
from tests.api.v1.conftest import SECRET
from tests.api.v1.conftest import build_settings as _settings
from tests.conftest import ACTIVE_USER_PASSWORD as PASSWORD

PROBLEM_CT = "application/problem+json"


@pytest.fixture
async def refresh_client(
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


async def _login(client: AsyncClient, user: User) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": PASSWORD},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


@pytest.mark.integration
async def test_refresh_returns_new_token_pair(refresh_client: AsyncClient, active_user: User) -> None:
    tokens = await _login(refresh_client, active_user)

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["expires_in"] == 3600
    assert body["access_token"]
    assert body["refresh_token"]

    session_user = decode_session_jwt(body["access_token"], secret=SECRET, algorithm="HS256")
    assert session_user.id == active_user.id
    assert session_user.provider == "local"


@pytest.mark.integration
async def test_refresh_reflects_updated_permissions(
    refresh_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
    member_role: Role,
) -> None:
    tokens = await _login(refresh_client, active_user)
    issued = decode_session_jwt(tokens["access_token"], secret=SECRET, algorithm="HS256")
    assert issued.permissions == frozenset()

    member_role.permissions = ["users:read"]
    await db_session.flush()

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == status.HTTP_200_OK
    refreshed = decode_session_jwt(response.json()["access_token"], secret=SECRET, algorithm="HS256")
    assert refreshed.permissions == frozenset({"users:read"})


@pytest.mark.integration
async def test_refresh_token_is_rotated_and_reusable(refresh_client: AsyncClient, active_user: User) -> None:
    tokens = await _login(refresh_client, active_user)

    first = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert first.status_code == status.HTTP_200_OK

    # The response carries a freshly minted refresh token, not the access token
    # echoed back. (Value-inequality with the input can't be asserted: a token
    # minted in the same second shares its `iat` and serialises identically —
    # there is no `jti`.) Decoding it as a refresh token proves rotation issued
    # a real refresh token, and using it for a second refresh proves it slides.
    rotated = first.json()["refresh_token"]
    rotated_identity = decode_refresh_jwt(rotated, secret=SECRET, algorithm="HS256")
    assert rotated_identity.id == active_user.id

    second = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": rotated},
    )
    assert second.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_refresh_preserves_auth_time_across_rotation(refresh_client: AsyncClient, active_user: User) -> None:
    tokens = await _login(refresh_client, active_user)
    original_auth_time = decode_refresh_jwt(tokens["refresh_token"], secret=SECRET, algorithm="HS256").auth_time
    assert original_auth_time is not None

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == status.HTTP_200_OK
    # The absolute ceiling only works if rotation carries the original login
    # instant forward instead of resetting it to "now".
    rotated_auth_time = decode_refresh_jwt(response.json()["refresh_token"], secret=SECRET, algorithm="HS256").auth_time
    assert rotated_auth_time == original_auth_time


@pytest.mark.integration
async def test_refresh_rejects_token_past_absolute_lifetime(refresh_client: AsyncClient, active_user: User) -> None:
    # A token whose original login is older than the absolute ceiling, but whose
    # own `exp` is still in the future — only the `auth_time` cap should reject it.
    settings = _settings()
    stale_auth_time = int(time.time()) - settings.refresh_absolute_max_lifetime_seconds - 1
    stale_token = encode_refresh_jwt(
        active_user,
        provider="local",
        secret=SECRET,
        algorithm="HS256",
        ttl_seconds=settings.refresh_token_ttl_seconds,
        auth_time=stale_auth_time,
    )

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": stale_token},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid or expired refresh token."


@pytest.mark.integration
async def test_refresh_rejects_access_token(refresh_client: AsyncClient, active_user: User) -> None:
    tokens = await _login(refresh_client, active_user)

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["access_token"]},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid or expired refresh token."


@pytest.mark.integration
async def test_refresh_rejects_garbage_token(refresh_client: AsyncClient) -> None:
    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not-a-jwt"},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid or expired refresh token."


@pytest.mark.integration
async def test_refresh_rejects_deactivated_user(
    refresh_client: AsyncClient,
    db_session: AsyncSession,
    active_user: User,
) -> None:
    tokens = await _login(refresh_client, active_user)

    active_user.status = UserStatus.INACTIVE
    await db_session.flush()

    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid or expired refresh token."


@pytest.mark.integration
async def test_refresh_rejects_force_logged_out_session(refresh_client: AsyncClient, active_user: User) -> None:
    tokens = await _login(refresh_client, active_user)

    await revoke_user_sessions(active_user.id)
    try:
        response = await refresh_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
    finally:
        async with session_revocation._redis() as client:
            await client.delete(session_revocation._key(active_user.id))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_CT
    assert response.json()["detail"] == "Invalid or expired refresh token."


@pytest.mark.integration
async def test_refresh_fails_closed_when_revocation_check_errors(
    refresh_client: AsyncClient, active_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Redis failure during the refresh revocation check must not silently mint a
    # fresh pair (which would escape a marker set moments earlier) — it fails loud.
    tokens = await _login(refresh_client, active_user)

    class _BoomClient:
        async def get(self, *_args: object, **_kwargs: object) -> str:
            raise RedisError("redis down")

    @asynccontextmanager
    async def _boom() -> AsyncIterator[_BoomClient]:
        yield _BoomClient()

    monkeypatch.setattr(session_revocation, "_redis", _boom)

    # Fail-loud: the outage surfaces as 503 (transient dependency) instead of the
    # endpoint returning a freshly minted pair.
    response = await refresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
@pytest.mark.parametrize("payload", [{"refresh_token": ""}, {}])
async def test_refresh_rejects_invalid_payload(refresh_client: AsyncClient, payload: dict[str, str]) -> None:
    response = await refresh_client.post("/api/v1/auth/refresh", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"] == PROBLEM_CT
    body = response.json()
    assert body["errors"]
    assert any("refresh_token" in error["loc"] for error in body["errors"])
