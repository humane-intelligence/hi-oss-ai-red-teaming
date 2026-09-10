"""Redis-backed per-user session revocation."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.auth.services import session_revocation
from app.core.auth.services.session_revocation import is_revoked
from app.core.auth.services.session_revocation import revoke_user_sessions


async def _clear(user_id: UUID) -> None:
    async with session_revocation._redis() as client:
        await client.delete(session_revocation._key(user_id))


@pytest.mark.unit
async def test_is_revoked_false_when_issued_at_missing() -> None:
    assert await is_revoked(uuid4(), None) is False


@pytest.mark.unit
async def test_shared_client_reused_and_closed_on_shutdown() -> None:
    closed = {"count": 0}

    class _FakeClient:
        async def aclose(self) -> None:
            closed["count"] += 1

    session_revocation._shared["client"] = cast(Redis, _FakeClient())
    try:
        async with session_revocation._redis() as client:
            assert client is session_revocation._shared["client"]
        assert closed["count"] == 0  # pooled client is not closed per call

        await session_revocation.close_client()
        assert closed["count"] == 1  # closed once, on lifespan shutdown
        assert "client" not in session_revocation._shared
    finally:
        session_revocation._shared.pop("client", None)


@pytest.mark.unit
async def test_is_revoked_fails_open_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        async def get(self, *_args: object, **_kwargs: object) -> str:
            raise RedisError("redis down")

    @asynccontextmanager
    async def _boom() -> AsyncIterator[_BoomClient]:
        yield _BoomClient()

    monkeypatch.setattr(session_revocation, "_redis", _boom)
    assert await is_revoked(uuid4(), issued_at=int(time.time())) is False


@pytest.mark.unit
async def test_is_revoked_reraises_on_redis_error_when_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        async def get(self, *_args: object, **_kwargs: object) -> str:
            raise RedisError("redis down")

    @asynccontextmanager
    async def _boom() -> AsyncIterator[_BoomClient]:
        yield _BoomClient()

    monkeypatch.setattr(session_revocation, "_redis", _boom)
    with pytest.raises(RedisError):
        await is_revoked(uuid4(), issued_at=int(time.time()), fail_open=False)


@pytest.mark.integration
async def test_is_revoked_false_without_marker() -> None:
    assert await is_revoked(uuid4(), issued_at=int(time.time())) is False


@pytest.mark.integration
async def test_revoke_then_older_token_is_revoked() -> None:
    user_id = uuid4()
    try:
        await revoke_user_sessions(user_id)
        assert await is_revoked(user_id, issued_at=int(time.time()) - 60) is True
    finally:
        await _clear(user_id)


@pytest.mark.integration
async def test_token_minted_at_revoke_instant_is_revoked() -> None:
    user_id = uuid4()
    try:
        await revoke_user_sessions(user_id)
        async with session_revocation._redis() as client:
            raw = await client.get(session_revocation._key(user_id))
        assert raw is not None
        assert await is_revoked(user_id, issued_at=int(raw)) is True
    finally:
        await _clear(user_id)


@pytest.mark.integration
async def test_token_minted_after_revoke_survives() -> None:
    user_id = uuid4()
    try:
        await revoke_user_sessions(user_id)
        assert await is_revoked(user_id, issued_at=int(time.time()) + 60) is False
    finally:
        await _clear(user_id)
