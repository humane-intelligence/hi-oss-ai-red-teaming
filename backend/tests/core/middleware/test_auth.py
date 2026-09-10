"""AuthMiddleware — populates request.state.user from Authorization: Bearer ..."""

from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi import Request
from httpx import ASGITransport
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.auth.models import User
from app.core.auth.services.jwt import encode_session_jwt
from app.core.config import Settings
from app.core.config import get_settings
from app.core.middleware.auth import AuthMiddleware

SECRET = "test-secret-comfortably-long-for-hs256"


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
    )


def _user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        email="ada@example.com",
        email_verified_at=now,
        first_name="Ada",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    settings = _settings()
    monkeypatch.setattr("app.core.middleware.auth.get_settings", lambda: settings)
    get_settings.cache_clear()

    fastapi_app = FastAPI()
    fastapi_app.add_middleware(AuthMiddleware)

    @fastapi_app.get("/whoami")
    def whoami(request: Request) -> dict[str, object]:
        user = getattr(request.state, "user", None)
        if user is None:
            return {"authenticated": False}
        return {"authenticated": True, "email": user.email}

    return fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.integration
async def test_no_authorization_header_yields_anonymous_request(client: AsyncClient) -> None:
    response = await client.get("/whoami")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


@pytest.mark.integration
async def test_valid_bearer_token_attaches_session_user(client: AsyncClient) -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm="HS256", ttl_seconds=3600)

    response = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "email": "ada@example.com"}


@pytest.mark.integration
async def test_tampered_token_falls_through_as_anonymous(client: AsyncClient) -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm="HS256", ttl_seconds=3600)
    tampered = token[:-2] + "AA"

    response = await client.get("/whoami", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


@pytest.mark.integration
async def test_expired_token_falls_through_as_anonymous(client: AsyncClient) -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm="HS256", ttl_seconds=-1)

    response = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


@pytest.mark.integration
async def test_non_bearer_scheme_is_ignored(client: AsyncClient) -> None:
    response = await client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


@pytest.mark.integration
async def test_revoked_token_falls_through_as_anonymous(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _revoked(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("app.core.middleware.auth.is_revoked", _revoked)
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm="HS256", ttl_seconds=3600)

    response = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
