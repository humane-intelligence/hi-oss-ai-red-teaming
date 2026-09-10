"""Shared helpers for `tests/api/v1` route tests.

`as_user` and `make_role` were copied verbatim across the evaluation-group route
tests; they live here so both modules import one definition. The `_user` / `_group`
builders stay module-local — their signatures are deliberately file-specific
(one seeds permission lists, the other takes a pre-built role / richer group
fields), so consolidating them would only add indirection.

`SECRET` / `PROBLEM_CT` / `build_settings` / `make_token` and the
`_configured_settings` / `auth_db_client` fixtures back the bearer-token route
tests (auth, role/permission catalogs) — one definition instead of a per-file copy.
"""

from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import current_user
from app.core.auth.dependencies import optional_current_user
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services import providers as providers_mod
from app.core.auth.services.jwt import encode_session_jwt
from app.core.auth.services.users import create_user
from app.core.config import Settings
from app.core.config import get_settings
from app.core.database import get_db
from app.main import app
from tests.conftest import session_user_from

SECRET = "test-secret-comfortably-long-for-hs256"
PROBLEM_CT = "application/problem+json"


@contextmanager
def as_user(user: User) -> Iterator[None]:
    """Override the `current_user` + `optional_current_user` dependencies to authenticate as `user`."""
    app.dependency_overrides[current_user] = lambda: session_user_from(user)
    app.dependency_overrides[optional_current_user] = lambda: session_user_from(user)
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(optional_current_user, None)


async def make_role(db: AsyncSession, permissions: list[str] | None = None) -> Role:
    """A throwaway `Role` with a unique name carrying `permissions` (empty by default)."""
    role = Role(name=f"role-{uuid4().hex[:8]}", description="test", permissions=permissions or [])
    db.add(role)
    await db.flush()
    await db.refresh(role)
    return role


def build_settings() -> Settings:
    # Services that from-import get_settings (e.g. registration/invitations) bypass the
    # _configured_settings monkeypatch and read the env-pinned Settings from tests/conftest.py
    # instead — keep the overlapping values here (email_from, frontend_base_url, TTLs) aligned
    # with that env block or those flows see a different config than the routes.
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
        oauth_state_secret=SecretStr("oauth-state-secret-for-tests-only"),
        oauth_cookie_secure=False,
        oauth_redirect_base_url="http://testserver",
        # Superset of what the per-file copies pinned before consolidation:
        model_secrets_key=SecretStr("test-model-secrets-key-at-least-32-chars"),
        email_from="noreply@example.com",
        frontend_base_url="http://localhost:3000",
        email_verification_ttl_hours=24,
        password_reset_ttl_hours=24,
        invitation_ttl_hours=168,
        bulk_max_rows=1000,
        refresh_absolute_max_lifetime_seconds=7_776_000,
    )


def make_token(user: User, *, provider: str = "local") -> str:
    """A signed access token for `user` over the test `SECRET`."""
    token, _ = encode_session_jwt(user, provider=provider, secret=SECRET, algorithm="HS256", ttl_seconds=3600)
    return token


def bearer(user: User) -> dict[str, str]:
    """Authorization headers for `user` — the one place the header shape lives."""
    return {"Authorization": f"Bearer {make_token(user)}"}


async def caller_with(db: AsyncSession, *permissions: Permission, email: str | None = None) -> User:
    """A fresh user holding exactly `permissions`, via a throwaway role.

    The shared replacement for the per-file `reader_role`/`updater_role` + `_caller`
    pairs: `await caller_with(db, Permission.EVALUATIONS_READ)`.
    """
    role = await make_role(db, [p.value for p in permissions])
    return await create_user(db, email=email or f"caller-{uuid4().hex[:8]}@example.com", roles=[role])


@pytest.fixture
def _configured_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    settings = build_settings()
    get_settings.cache_clear()
    providers_mod.get_oauth.cache_clear()
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.middleware.auth.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    providers_mod.get_oauth.cache_clear()


@pytest.fixture
async def auth_db_client(_configured_settings: Settings, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """`get_db` wired to the test session — for DB-backed bearer routes (`/me`, catalogs)."""
    app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
