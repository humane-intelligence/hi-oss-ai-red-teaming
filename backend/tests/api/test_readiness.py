"""Tests for /ready — readiness probe composing Postgres + Redis health checks."""

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator

import asyncpg
import pytest
from fastapi import status
from httpx import AsyncClient
from redis.asyncio import Redis

import app.api.health as health_module
from app.api.health import CheckResult
from app.api.health import _check_postgres
from app.api.health import _check_redis
from app.core.config import Settings
from app.core.config import get_settings
from app.main import app

type CheckFunc = Callable[[Settings], Awaitable[CheckResult]]


def _stub_check(result: CheckResult) -> CheckFunc:
    async def _check(_settings: Settings) -> CheckResult:
        return result

    return _check


class _FakeRedis:
    def __init__(self, ping_error: Exception | None = None) -> None:
        self._ping_error = ping_error
        self.closed = False

    async def ping(self) -> bool:
        if self._ping_error is not None:
            raise self._ping_error
        return True

    async def aclose(self) -> None:
        self.closed = True


class _FakePostgresConn:
    def __init__(self, execute_error: Exception | None = None) -> None:
        self._execute_error = execute_error
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, query: str) -> None:
        self.executed.append(query)
        if self._execute_error is not None:
            raise self._execute_error

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _override_settings() -> Iterator[None]:
    """Bypass env-driven Settings so /ready can be exercised without a real .env."""
    app.dependency_overrides[get_settings] = lambda: Settings.model_construct(
        database_host="localhost",
        database_port=5432,
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="localhost",
        redis_port=6379,
    )
    yield
    app.dependency_overrides.pop(get_settings, None)


@pytest.mark.unit
async def test_readiness_returns_ok_when_all_checks_pass(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_module, "_check_postgres", _stub_check(CheckResult(ok=True)))
    monkeypatch.setattr(health_module, "_check_redis", _stub_check(CheckResult(ok=True)))

    response = await async_client.get("/ready")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "status": "ok",
        "checks": {
            "postgres": {"ok": True, "error": None},
            "redis": {"ok": True, "error": None},
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("postgres", "redis"),
    [
        (CheckResult(ok=False, error="pg down"), CheckResult(ok=True)),
        (CheckResult(ok=True), CheckResult(ok=False, error="redis down")),
        (CheckResult(ok=False, error="pg down"), CheckResult(ok=False, error="redis down")),
    ],
    ids=["postgres_fails", "redis_fails", "both_fail"],
)
async def test_readiness_returns_503_when_any_check_fails(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    postgres: CheckResult,
    redis: CheckResult,
) -> None:
    monkeypatch.setattr(health_module, "_check_postgres", _stub_check(postgres))
    monkeypatch.setattr(health_module, "_check_redis", _stub_check(redis))

    response = await async_client.get("/ready")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["postgres"] == {"ok": postgres.ok, "error": postgres.error}
    assert body["checks"]["redis"] == {"ok": redis.ok, "error": redis.error}


@pytest.mark.unit
async def test_check_postgres_returns_ok_on_success(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = _FakePostgresConn()

    async def fake_connect(**_kwargs: object) -> _FakePostgresConn:
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    result = await _check_postgres(settings)

    assert result == CheckResult(ok=True, error=None)
    assert fake_conn.executed == ["SELECT 1"]
    assert fake_conn.closed is True


@pytest.mark.unit
async def test_check_postgres_returns_error_when_connect_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_connect(**_kwargs: object) -> _FakePostgresConn:
        raise OSError("connection refused")

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    result = await _check_postgres(settings)

    assert result.ok is False
    assert result.error == "connection refused"


@pytest.mark.unit
async def test_check_postgres_returns_error_when_query_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_conn = _FakePostgresConn(execute_error=RuntimeError("query boom"))

    async def fake_connect(**_kwargs: object) -> _FakePostgresConn:
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    result = await _check_postgres(settings)

    assert result.ok is False
    assert result.error == "query boom"
    assert fake_conn.closed is True


@pytest.mark.unit
async def test_check_redis_returns_ok_on_success(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(Redis, "from_url", classmethod(lambda _cls, *_a, **_kw: fake))

    result = await _check_redis(settings)

    assert result == CheckResult(ok=True, error=None)
    assert fake.closed is True


@pytest.mark.unit
async def test_check_redis_returns_error_when_ping_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis(ping_error=ConnectionError("redis down"))
    monkeypatch.setattr(Redis, "from_url", classmethod(lambda _cls, *_a, **_kw: fake))

    result = await _check_redis(settings)

    assert result.ok is False
    assert result.error == "redis down"
    assert fake.closed is True
