"""Shared pytest fixtures for the FastAPI app.

Test database isolation
-----------------------
DB integration tests NEVER touch the local or production database.  The
`_test_db` session fixture creates a dedicated `<DATABASE_NAME>_test` database
on the same Postgres instance, runs Alembic migrations exactly once per session,
then drops the database on teardown.  Under pytest-xdist each worker gets its
own database (`..._test_gw0`, `..._test_gw1`, …) — create + migrate cost ~1 s
per worker, so workers stay fully isolated on the DB side.  Redis is NOT
per-worker; tests touching real Redis must isolate their keyspace.

Per-test isolation is achieved via SAVEPOINT: `db_session` wraps each test in an
outer transaction that is always rolled back, so every test starts with a clean
schema regardless of what the previous test did.  The session uses
join_transaction_mode="create_savepoint" so that any commit() call inside
production code creates a nested savepoint instead of committing for real.

Dependency override pattern
----------------------------
Endpoints that inject `DbSession` must be tested with `async_client_with_db`,
which overrides `get_db` with a lambda returning the test session.  Plain
`async_client` (no DB override) is still available for tests that mock the DB
layer or do not touch it at all.
"""

import os

# Test settings are pinned here, not read from a developer's local `.env`.
# Set BEFORE any `from app.*` import so Settings sees these values during its
# first instantiation. DB / Redis credentials match `.env.example` because
# the compose db container is provisioned from `.env` (copied from
# `.env.example` by `make services`); keep them aligned or tests can't connect.
os.environ.update(
    {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "postgres",
        "DATABASE_NAME": "ai_red_teaming",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        # Without this, tests inherit `.env`'s compose-only `redis://redis:...` and stall on DNS retries.
        "CELERY_BROKER_URL": "memory://",
        "SESSION_JWT_SECRET": "test-session-jwt-secret-at-least-32-chars",
        "OAUTH_STATE_SECRET": "test-oauth-state-secret-at-least-32-chars",
        "MODEL_SECRETS_KEY": "test-model-secrets-key-at-least-32-chars",
        # Required, and deliberately never the model key — `Settings` refuses shared material.
        "CONVERSATION_SECRETS_KEY": "test-conversation-secrets-key-at-least-32-chars",
        "OAUTH_REDIRECT_BASE_URL": "http://localhost:8000",
        "OAUTH_COOKIE_SECURE": "false",
        # `providers.py` from-imports `get_settings`, so it reads these env-pinned
        # values rather than a fixture's Settings override — the OIDC suites need
        # google registered to exercise the login/callback routes at all.
        "OIDC_GOOGLE_CLIENT_ID": "test-google-client-id",
        "OIDC_GOOGLE_CLIENT_SECRET": "test-google-client-secret",
        "CORS_ORIGINS": '["http://localhost:3000"]',
        "EMAIL_BACKEND": "console",
        "EMAIL_FROM": "noreply@example.com",
        "FRONTEND_BASE_URL": "http://localhost:3000",
        "LOG_FORMAT": "json",
        "ENVIRONMENT": "test",
        # Without this, a real SENTRY_DSN left in a developer's `.env` would make the
        # suite actually talk to Sentry (network calls under pytest-socket, real events).
        "SENTRY_DSN": "",
    },
)

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import structlog
from alembic.config import Config as AlembicConfig
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from httpx import ASGITransport
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.engine import Connection as SyncConnection
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import col

from alembic import command as alembic_command
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import SessionUser
from app.core.auth.services import passwords
from app.core.auth.services.roles import sync_system_roles
from app.core.auth.services.users import create_user
from app.core.config import Settings
from app.core.config import get_settings
from app.core.database import get_db
from app.core.email import tasks as email_tasks_module
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.licenses.service import sync_licenses
from app.core.logging import configure_logging
from app.core.media.services.images import clear_data_url_cache
from app.main import app
from app.workers.celery_app import app as celery_app

# Disable `.env` loading so a developer's local file can never bleed into the
# test process. The env vars set above are the sole source of truth.
Settings.model_config["env_file"] = None
# litellm (pulled in transitively by app.main above) calls load_dotenv() at
# import, copying the dev `.env` into os.environ — which the pydantic env source
# reads regardless of env_file. Drop the email/SMTP keys it can leak (the ones
# we don't pin above) so they fall back to the Settings defaults in tests.
for _leaked in (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_TLS",
    "AWS_REGION",
    "BRAND_COMPANY",
    "BRAND_PRODUCT",
    "EMAIL_FOOTER_ADDRESS",
):
    os.environ.pop(_leaked, None)
get_settings.cache_clear()

# ASGITransport skips the lifespan, so wire structlog here or it falls back to slow rich tracebacks.
configure_logging(get_settings())

# Argon2's OWASP cost (~66 ms/hash) is pure waste in tests; cheap params still verify. Prod untouched.
passwords._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

# ---------------------------------------------------------------------------
# Generic app fixtures (no real DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def _reset_logging() -> Iterator[None]:
    """Snapshot and restore root-handler + structlog state around a `configure_logging` call."""
    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    saved_config = structlog.get_config()
    try:
        yield
    finally:
        logging.getLogger().handlers = saved_handlers
        logging.getLogger().setLevel(saved_level)
        structlog.configure(**saved_config)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Synchronous TestClient — fine for simple request/response checks."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Async httpx client wired to the ASGI app.

    Does NOT trigger the FastAPI lifespan, so get_db is not available unless
    overridden.  Use async_client_with_db for endpoints that touch the database.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def eager_celery() -> Iterator[None]:
    """Run Celery tasks in-process for tests.

    Apply explicitly via ``@pytest.mark.usefixtures("eager_celery")``. Without it,
    the session-pinned ``memory://`` broker (env block above) enqueues a task but
    never runs it; a real-broker test (smoke / e2e) must override that pin too.
    """
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


@pytest.fixture
def celery_enqueue_stub(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Swap the email enqueue for a mock, so a route's mail dispatch is assertable as a call count.

    The broker is already inert (`memory://`, pinned session-wide above) — what this buys is the
    spy, not isolation. One definition instead of the per-module copies that drifted into two
    spellings.
    """
    enqueue = MagicMock()
    monkeypatch.setattr(email_tasks_module.send_email_task, "apply_async", enqueue)
    return enqueue


@pytest.fixture
def settings() -> Settings:
    """Fake Settings instance for unit tests — never connects to a real service."""
    return Settings.model_construct(
        database_host="localhost",
        database_port=5431,
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="localhost",
        redis_port=6378,
    )


def make_settings(**overrides: Any) -> Settings:
    """Build a fake `Settings` with per-test overrides — never connects to a real service.

    `model_construct` skips validation, so callers pass only what they exercise and ignore
    the rest; the base is a superset of the DB/redis/logging/error-reporting fields the
    unit suites need.
    """
    base: dict[str, Any] = {
        "database_host": "localhost",
        "database_port": 5432,
        "database_user": "user",
        "database_password": "pass",
        "database_name": "db",
        "redis_host": "localhost",
        "redis_port": 6379,
        "environment": "test",
        "git_sha": "abc1234",
        "log_level": "INFO",
        "log_format": "json",
        "sentry_dsn": None,
    }
    return Settings.model_construct(**(base | overrides))


# ---------------------------------------------------------------------------
# Database fixtures — isolated test DB, never touches local/prod
# ---------------------------------------------------------------------------


def _validate_db_name(name: str) -> None:
    """Guard against SQL injection in the DB name used for CREATE/DROP DATABASE."""
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", name):
        msg = f"Unsafe database name rejected: {name!r}"
        raise ValueError(msg)


@pytest.fixture(scope="session")
def _test_db() -> Iterator[str]:
    """Session-scoped fixture: creates a fresh test DB, drops it on teardown.

    Runs asyncpg admin operations through asyncio.run so this stays a plain
    (non-async) session fixture and does not contend with pytest-asyncio's
    per-test event loop.

    The test DB name is derived from DATABASE_NAME + '_test', so it automatically
    tracks any environment-level override without extra config.
    """
    real_settings = get_settings()
    # xdist workers each get their own DB; empty suffix = single-process run (-n 0).
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    suffix = f"_{worker}" if worker else ""
    test_db_name = f"{real_settings.database_name}_test{suffix}"
    _validate_db_name(test_db_name)

    # CREATE/DROP DATABASE can't be parameterised and can't run in a
    # transaction; the regex guard above is what makes interpolation safe here.
    quoted = f'"{test_db_name}"'
    drop_stmt = f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"
    create_stmt = f"CREATE DATABASE {quoted}"

    async def _admin_exec(*statements: str) -> None:
        conn = await asyncpg.connect(
            host=real_settings.database_host,
            port=real_settings.database_port,
            user=real_settings.database_user,
            password=real_settings.database_password,
            database="postgres",
        )
        try:
            for stmt in statements:
                await conn.execute(stmt)
        finally:
            await conn.close()

    asyncio.run(_admin_exec(drop_stmt, create_stmt))

    yield test_db_name

    asyncio.run(_admin_exec(drop_stmt))


@pytest.fixture(scope="session")
def db_engine(_test_db: str) -> Iterator[AsyncEngine]:
    """Session-scoped async engine pointing at the test DB, with migrations applied.

    NullPool is used so the engine has no loop affinity — each connect()
    opens a fresh asyncpg connection. This matters because the migrations
    step below runs inside its own asyncio.run() loop, separate from the
    per-test event loop that db_session later uses.
    """
    real_settings = get_settings()
    test_url = make_url(real_settings.database_url).set(database=_test_db).render_as_string(hide_password=False)
    engine = create_async_engine(test_url, poolclass=NullPool)

    def _run_alembic_migrations(sync_conn: SyncConnection) -> None:
        cfg = AlembicConfig("alembic.ini")
        cfg.attributes["connection"] = sync_conn
        alembic_command.upgrade(cfg, "head")

    async def _apply_migrations() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic_migrations)

    asyncio.run(_apply_migrations())

    async def _seed_reference_data() -> None:
        # Curated licences are reference data (like system roles): seed once per worker so
        # `get_default_license` and effective-licence resolution work across every test. Committed
        # here (outside the per-test rolled-back transaction) so all tests in the worker see them.
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await sync_licenses(session)
            await session.commit()

    asyncio.run(_seed_reference_data())

    yield engine

    asyncio.run(engine.dispose())


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Function-scoped session wrapped in a rolled-back transaction.

    Every test gets a clean slate:
      - An outer transaction is opened on the raw connection.
      - The session is bound to that connection with join_transaction_mode=
        "create_savepoint", so any commit() inside production code creates a
        SAVEPOINT that is released (not committed to disk) and stays visible
        within the test.
      - After the test, the outer transaction is rolled back — no data persists.
    """
    async with db_engine.connect() as conn, conn.begin() as outer:
        async_session = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with async_session() as session:
            yield session
        await outer.rollback()


@pytest_asyncio.fixture
async def async_client_with_db(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Async client with get_db overridden to the isolated test session.

    Use this fixture for endpoint tests that write to or read from the database.
    The override ensures no request ever reaches the production engine.
    """
    app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Auth fixtures — a baseline `member` role and an ACTIVE password-backed user.
# Shared across API-level and service-level login tests; constants are
# importable so test bodies can reference the plaintext password.
# ---------------------------------------------------------------------------

ACTIVE_USER_EMAIL = "ada@example.com"
ACTIVE_USER_PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def member_role(db_session: AsyncSession) -> Role:
    role = Role(name="member", description="Member", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def system_roles(db_session: AsyncSession) -> dict[str, Role]:
    """Seed the canonical system roles into the test session — `name -> Role`.

    Object-role flows resolve canonical roles by slug (e.g. the creator's
    auto-assigned `owner`, group-member assignment), so any test exercising those
    paths needs the rows `sync_system_roles` writes on deploy.
    """
    return await sync_system_roles(db_session)


@pytest_asyncio.fixture
async def active_user(db_session: AsyncSession, member_role: Role) -> User:
    return await create_user(
        db_session,
        email=ACTIVE_USER_EMAIL,
        password=SecretStr(ACTIVE_USER_PASSWORD),
        status=UserStatus.ACTIVE,
        email_verified=True,
        roles=[member_role],
    )


async def persist_evaluation_group(  # noqa: PLR0913 — keyword-only args mirror the group's column set, like the service helpers
    db_session: AsyncSession,
    *,
    title: str = "Engagement",
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    created_by_id: UUID | None = None,
    # Default to `approved` — the earliest state that accepts child evaluations
    # (the add paths 409 on any pre-approval state), and non-draft, so a `public`
    # group stays non-member-visible (a `public` *draft* is owner/manager-only).
    # Tests needing a specific lifecycle state (publication/draft) pass `status`.
    status: PublicationStatus = PublicationStatus.APPROVED,
    start_date: date = date(2026, 3, 1),
    end_date: date | None = None,
    # Pinned to the most-restrictive `owner_only` for a deterministic baseline
    # (the product default is `members_personal_metrics`); metrics tests opt into
    # a wider level explicitly.
    metrics_access_during: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
    metrics_access_after: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
    data_license_id: UUID | None = None,
) -> EvaluationGroup:
    """Persist an `EvaluationGroup` (and the `User` it needs for `created_by_id`).

    Shared by the evaluation/assignment tests so each one can satisfy the
    `Evaluation.evaluation_group_id` FK without re-deriving the owner + group
    setup. Pass ``created_by_id`` to make an existing user (e.g. the calling
    user) the owner — needed since evaluation mutations are gated on the parent
    group's in-group roles; otherwise a throwaway owner is created.

    Mirrors the real create path by granting the creator the in-group `owner`
    role (the sole source of their object authority — there is no `created_by_id`
    fallback), seeding the canonical roles first if the test hasn't already.
    """
    if created_by_id is None:
        owner = User(email=f"{uuid4().hex[:8]}@example.com")
        db_session.add(owner)
        await db_session.flush()
        created_by_id = owner.id
    group = EvaluationGroup(
        title=title,
        description="A red-teaming engagement.",
        created_by_id=created_by_id,
        access_level=access_level,
        status=status,
        start_date=start_date,
        end_date=end_date,
        metrics_access_during=metrics_access_during,
        metrics_access_after=metrics_access_after,
        data_license_id=data_license_id,
    )
    db_session.add(group)
    await db_session.flush()
    await db_session.refresh(group)
    owner_role = await ensure_owner_role(db_session)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, created_by_id, [owner_role])
    return group


async def ensure_owner_role(db_session: AsyncSession) -> Role:
    """The canonical `owner` role, syncing the system roles first if absent."""
    result = await db_session.execute(Role.live_select().where(col(Role.name) == SystemRole.OWNER.value))
    role = result.scalar_one_or_none()
    if role is None:
        role = (await sync_system_roles(db_session))[SystemRole.OWNER.value]
    return role


def session_user_from(user: User) -> SessionUser:
    """Build a `SessionUser` mirroring `user`'s identity and flattened role permissions.

    Centralised so service- and api-layer tests don't each carry a copy of the
    permission-flattening expression.
    """
    return SessionUser(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        first_name=user.first_name,
        last_name=user.last_name,
        provider="local",
        permissions=frozenset(perm for role in user.roles for perm in role.permissions),
    )


def finalize_session_provider(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """A finalize-session provider that yields the test session (no commit/close — the fixture owns it).

    Transaction B is detached in production (`standalone_session` needs the lifespan engine, absent in
    tests), so anything written at finalize is invisible unless a test routes it here.
    """

    @asynccontextmanager
    async def provider() -> AsyncIterator[AsyncSession]:
        yield session

    return provider


@pytest.fixture(autouse=True)
def _clear_data_url_cache():
    """The data-URL cache is process-global; clear it around every test so the random-order suite stays independent."""
    clear_data_url_cache()
    yield
    clear_data_url_cache()
