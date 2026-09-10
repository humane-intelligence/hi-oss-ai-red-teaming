"""Tests for the platform bulk-operations contract.

The helper (`apply_bulk`) is exercised against a mocked `AsyncSession`
so the unit tier covers commit / rollback / processor wiring without
booting Postgres. DB-backed integration coverage rides with the real
consumers, bulk user invitation first among them.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bulk import BulkRequest
from app.core.bulk import BulkResponse
from app.core.bulk import BulkRow
from app.core.bulk import apply_bulk
from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.schemas import ProblemErrorItem

pytestmark = pytest.mark.unit


class _Input(BaseModel):
    name: str


class _Output(BaseModel):
    id: int
    name: str


class _FakeSavepoint:
    """Stand-in for SQLAlchemy's `AsyncSessionTransaction` — async context manager."""

    async def __aenter__(self) -> _FakeSavepoint:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        # Don't suppress: an `APIError` raised inside must propagate out so
        # `apply_bulk` catches it after the savepoint has rolled back.
        return False


def _fake_session() -> AsyncMock:
    """Build an `AsyncSession` mock with a working `begin_nested()` context manager."""
    session = AsyncMock(spec=AsyncSession)
    # `_FakeSavepoint` is stateless, so a single shared instance across calls is fine.
    session.begin_nested = MagicMock(return_value=_FakeSavepoint())
    return session


def _row(key: str, name: str) -> BulkRow[_Input]:
    return BulkRow[_Input](row_key=key, data=_Input(name=name))


# ─── Schema validation ─────────────────────────────────────────────


def test_duplicate_row_keys_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        BulkRequest[_Input](rows=[_row("a", "x"), _row("a", "y")])

    assert "Duplicate row_key" in str(exc_info.value)


def test_empty_rows_rejected() -> None:
    with pytest.raises(ValidationError):
        BulkRequest[_Input](rows=[])


def test_exceeds_bulk_max_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BULK_MAX_ROWS", "2")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as exc_info:
            BulkRequest[_Input](rows=[_row(f"r{i}", str(i)) for i in range(3)])
        assert "exceeds limit" in str(exc_info.value)
    finally:
        get_settings.cache_clear()


def test_row_key_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        BulkRequest[_Input](rows=[BulkRow[_Input](row_key="", data=_Input(name="x"))])


def test_response_schema_compiles_for_two_level_generic() -> None:
    """Smoketest: nested generic (`list[BulkRowResult[_Output]]`) doesn't blow up Pydantic JSON Schema."""
    schema = BulkResponse[_Output].model_json_schema()

    assert "properties" in schema
    assert "results" in schema["properties"]
    # `$defs` should carry both _Output and BulkRowResult[_Output] entries.
    assert "$defs" in schema


# ─── apply_bulk ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_commits_and_records_ok_results() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](rows=[_row("a", "x"), _row("b", "y")])

    async def processor(_: AsyncSession, data: _Input) -> _Output:
        return _Output(id=1, name=data.name)

    response = await apply_bulk(session, request, processor)

    assert response.dry_run is False
    assert response.total == 2
    assert response.succeeded == 2
    assert response.failed == 0
    assert [r.status for r in response.results] == ["ok", "ok"]
    assert [r.row_key for r in response.results] == ["a", "b"]
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_records_problem_inline() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](
        rows=[_row("a", "ok-1"), _row("b", "boom"), _row("c", "ok-2")],
    )

    async def processor(_: AsyncSession, data: _Input) -> _Output:
        if data.name == "boom":
            raise ConflictError("dup name")
        return _Output(id=1, name=data.name)

    response = await apply_bulk(session, request, processor)

    assert response.succeeded == 2
    assert response.failed == 1
    assert [r.row_key for r in response.results] == ["a", "b", "c"]

    failed = response.results[1]
    assert failed.status == "failed"
    assert failed.data is None
    assert failed.error is not None
    assert failed.error.status == 409
    assert failed.error.title == "Conflict"
    assert failed.error.detail == "dup name"
    assert failed.error.instance is None

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_rewrites_errors_loc_to_be_row_relative() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](rows=[_row("a", "ok"), _row("b", "boom")])

    async def processor(_: AsyncSession, data: _Input) -> _Output:
        if data.name == "boom":
            conflict = ConflictError("dup name")
            conflict.errors = [ProblemErrorItem(loc=["body", "name"], msg="dup name", type="duplicate_name")]
            raise conflict
        return _Output(id=1, name=data.name)

    response = await apply_bulk(session, request, processor)

    failed = response.results[1]
    assert failed.error is not None
    # The real body is `{"rows": [...], ...}` — a bare `["body", "name"]` from the
    # single-item routes would point at a field that does not exist at this depth.
    assert failed.error.errors == [
        ProblemErrorItem(loc=["body", "rows", 1, "data", "name"], msg="dup name", type="duplicate_name")
    ]


@pytest.mark.asyncio
async def test_all_rows_fail() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](rows=[_row("a", "x"), _row("b", "y")])

    async def processor(_: AsyncSession, __: _Input) -> _Output:
        raise NotFoundError("nope")

    response = await apply_bulk(session, request, processor)

    assert response.succeeded == 0
    assert response.failed == 2
    assert all(r.status == "failed" for r in response.results)
    assert all(r.error is not None and r.error.status == 404 for r in response.results)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_rolls_back_on_full_success() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](rows=[_row("a", "x"), _row("b", "y")], dry_run=True)

    async def processor(_: AsyncSession, data: _Input) -> _Output:
        return _Output(id=1, name=data.name)

    response = await apply_bulk(session, request, processor)

    assert response.dry_run is True
    assert response.succeeded == 2
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_still_surfaces_per_row_failures() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](
        rows=[_row("a", "x"), _row("b", "boom")],
        dry_run=True,
    )

    async def processor(_: AsyncSession, data: _Input) -> _Output:
        if data.name == "boom":
            raise ConflictError("dup")
        return _Output(id=1, name=data.name)

    response = await apply_bulk(session, request, processor)

    assert response.dry_run is True
    assert response.succeeded == 1
    assert response.failed == 1
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_exception_aborts_whole_bulk() -> None:
    session = _fake_session()
    request = BulkRequest[_Input](rows=[_row("a", "x"), _row("b", "y")])

    async def processor(_: AsyncSession, __: _Input) -> _Output:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await apply_bulk(session, request, processor)

    # Neither commit nor rollback at the helper level — surrounding request
    # scope owns recovery for unexpected failures.
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
