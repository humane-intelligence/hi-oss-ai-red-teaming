"""Smoke tests for the test-DB fixture stack.

Validates _test_db / db_engine / db_session / async_client_with_db actually wire up:
the dedicated test DB is created, migrations apply, the session executes queries,
and SAVEPOINT isolation rolls back between tests (re-running this file must always
pass — if rollback broke, the CREATE TABLE below would collide on the second run).

Also covers the `transactional` decorator (pure-logic / mocked session).
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.database import transactional


@pytest.mark.integration
async def test_db_session_executes_query(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1"))

    assert result.scalar_one() == 1


@pytest.mark.integration
async def test_db_session_commit_becomes_savepoint(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TABLE smoke (id int PRIMARY KEY)"))
    await db_session.execute(text("INSERT INTO smoke VALUES (1)"))
    await db_session.commit()

    result = await db_session.execute(text("SELECT count(*) FROM smoke"))
    assert result.scalar_one() == 1


@pytest.mark.integration
async def test_async_client_with_db_serves_request(async_client_with_db: AsyncClient) -> None:
    response = await async_client_with_db.get("/health")

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_transactional_rolls_back_on_integrity_error(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TABLE smoke (id int PRIMARY KEY)"))
    await db_session.execute(text("INSERT INTO smoke VALUES (1)"))
    await db_session.commit()

    @transactional
    async def handler(*, db: AsyncSession) -> None:
        await db.execute(text("INSERT INTO smoke VALUES (1)"))

    with pytest.raises(IntegrityError):
        await handler(db=db_session)

    result = await db_session.execute(text("SELECT count(*) FROM smoke"))
    assert result.scalar_one() == 1


@pytest.mark.unit
async def test_transactional_commits_on_success() -> None:
    session = AsyncMock(spec=AsyncSession)

    @transactional
    async def handler(*, db: AsyncSession) -> str:
        return "ok"

    result = await handler(db=session)

    assert result == "ok"
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.unit
async def test_transactional_rolls_back_on_exception() -> None:
    session = AsyncMock(spec=AsyncSession)

    @transactional
    async def handler(*, db: AsyncSession) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await handler(db=session)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.unit
async def test_transactional_requires_db_kwarg() -> None:
    @transactional
    async def handler() -> None:
        return None

    with pytest.raises(TypeError, match="'db' kwarg"):
        await handler()


class _RecordingSession:
    """Minimal async-CM session that records commit/rollback — no engine needed."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.unit
async def test_standalone_session_raises_when_uninitialised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "_session_factory", None)

    with pytest.raises(RuntimeError, match="not initialised"):
        async with database.standalone_session():
            pass


@pytest.mark.unit
async def test_standalone_session_commits_on_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _RecordingSession()
    monkeypatch.setattr(database, "_session_factory", lambda: session)

    async with database.standalone_session() as yielded:
        assert yielded is session

    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.unit
async def test_standalone_session_rolls_back_and_reraises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _RecordingSession()
    monkeypatch.setattr(database, "_session_factory", lambda: session)

    with pytest.raises(ValueError, match="boom"):
        async with database.standalone_session():
            raise ValueError("boom")

    assert session.rolled_back is True
    assert session.committed is False
