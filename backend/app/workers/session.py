"""Sync SQLAlchemy session for Celery tasks.

Celery tasks run in worker processes that have no asyncio event loop, so the
async engine from app.core.database is unusable here.  This module mirrors
that engine on the sync side via the psycopg (v3) driver and exposes a
``session_scope()`` context manager.

Engine and sessionmaker are lazy module-level singletons: the first
``session_scope()`` call inside a worker process builds them; every subsequent
task in the same process reuses them.  No explicit lifecycle is needed —
Celery handles worker startup/shutdown and the connection pool drains on exit.
"""

from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_sync_engine(settings: Settings) -> Engine:
    """Sync engine for worker use; pool tuning mirrors the async engine."""
    return create_engine(
        settings.database_url_sync,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        echo=settings.db_echo,
    )


def build_async_engine(settings: Settings) -> AsyncEngine:
    """Async engine for the few worker tasks that reuse the async service layer.

    Mirrors `app.core.database.build_engine` deliberately rather than importing it
    (the tasks convention keeps worker modules off `app/core/database.py` and its
    loop-bound singletons). Uses `NullPool`: this engine is built and disposed once per
    task run (an async engine can't outlive its `asyncio.run` loop), so a connection
    pool would only add setup/warm-up churn with nothing to reuse — NullPool opens one
    connection for the run and closes it on dispose.

    Left at the default READ COMMITTED: an engine-wide REPEATABLE READ was tried to give
    exports a stable read snapshot, but it made the reaper's soft-delete and the export's
    own `READY` commit prone to unretried serialization failures (40001) and pinned a
    long snapshot across the multi-minute upload. See `iter_pages` for the OFFSET-paging
    consistency trade-off that choice was meant to address.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=settings.db_echo,
    )


def _ensure_session_factory() -> sessionmaker[Session]:
    global _engine, _session_factory  # noqa: PLW0603 — lazy per-worker singletons
    if _session_factory is None:
        _engine = build_sync_engine(get_settings())
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a Session bound to a transaction; commit on success, rollback on error."""
    factory = _ensure_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession for a task that must reuse the async service layer.

    Most tasks use the sync `session_scope`. This exists for the few that have to
    call async, DB-bound services unchanged (the export path re-runs the same
    visibility resolution and row fetchers the HTTP handlers use) — driving them
    from a worker means an event loop, so the task wraps its body in `asyncio.run`
    and consumes this scope inside it.

    The engine is built and disposed per call, not cached like the sync one: an
    async engine is bound to the loop that created it, and each `asyncio.run` spins
    a fresh loop — a module-level singleton would bind to a dead loop on the next
    task. Transaction boundaries are the caller's (no auto-commit): a long export
    generation shouldn't sit inside one open transaction, so the task commits its
    state transitions explicitly and this scope only rolls back / disposes on exit.
    """
    engine = build_async_engine(get_settings())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()
