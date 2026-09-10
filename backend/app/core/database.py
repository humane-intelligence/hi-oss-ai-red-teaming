"""Async SQLAlchemy engine and session factory.

Lifecycle
---------
_engine and _session_factory are module-level singletons set by init_engine
during the FastAPI lifespan (app/main.py).  Tests bypass the lifespan entirely
and override the get_db dependency to inject a transaction-bound session
(see tests/conftest.py).

Naming convention for constraints lives in app.core.base_model, which is
imported transitively whenever any model is loaded — so it is in place before
any table=True declaration.

Usage in route handlers
-----------------------
    from app.core.dependencies import DbSession

    async def my_route(db: DbSession) -> ...:
        result = await db.execute(select(MyModel))
"""

import functools
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

# Opens a session for work outside the request lifecycle; `standalone_session` in
# production, the test session in tests.
SessionProvider = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def build_engine(settings: Settings) -> AsyncEngine:
    """Create an async SQLAlchemy engine wired with the configured pool.

    Args:
        settings: Application settings carrying ``database_url`` and the
            ``db_pool_*`` / ``db_echo`` fields.

    Returns:
        A fresh `AsyncEngine`. The caller owns its lifecycle and must call
        `AsyncEngine.dispose` on shutdown.
    """
    # pool_pre_ping is always on: it issues SELECT 1 before reusing a
    # connection, so we transparently survive DB restarts and short
    # network blips. All other pool params come from Settings (see README).
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        echo=settings.db_echo,
    )


# Lifespan-managed singletons; tests override get_db rather than touching these.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(settings: Settings) -> None:
    """Build and install the module-level engine and session factory.

    Called once from the FastAPI lifespan on startup. Reassigns the
    singletons unconditionally — calling it twice replaces the previous
    engine without disposing it, which is only safe in tests that build a
    fresh `Settings`.

    Args:
        settings: Settings used to construct the engine.
    """
    global _engine, _session_factory  # noqa: PLW0603 — lifespan-managed singletons
    _engine = build_engine(settings)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_engine() -> None:
    """Dispose the engine and clear module-level state.

    Idempotent — safe to call when no engine has been initialised, or to
    call twice during a botched startup. The module-level handles are
    always cleared, even if `AsyncEngine.dispose` raises.
    """
    global _engine, _session_factory  # noqa: PLW0603 — lifespan-managed singletons
    try:
        if _engine is not None:
            await _engine.dispose()
    finally:
        _engine = None
        _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield one async session per request — FastAPI dependency.

    The session is closed automatically when the request finishes (success
    or failure). Routes should consume it via the `DbSession` alias from
    [app/core/dependencies.py](dependencies.py), not by depending on
    `get_db` directly.

    Yields:
        A fresh `AsyncSession` bound to the lifespan-managed engine.

    Raises:
        RuntimeError: If the engine has not been initialised — typically
            means the FastAPI lifespan did not run (e.g. a test bypassing
            the lifespan without overriding `get_db`).
    """
    if _session_factory is None:
        raise RuntimeError(
            "Database not initialised — the FastAPI lifespan must run before any "
            "endpoint that uses get_db.  In tests, override get_db via "
            "app.dependency_overrides instead of relying on the lifespan."
        )
    async with _session_factory() as session:
        yield session


@asynccontextmanager
async def standalone_session() -> AsyncIterator[AsyncSession]:
    """A fresh session outside the request lifecycle, committed on clean exit.

    For work that runs *after* the request session has been released — e.g.
    finalizing a streamed assistant message once the (possibly long) generation
    ends, where the request connection was returned to the pool before streaming
    (the A/B write-path split). Commits on a clean exit, rolls back on error. Not a
    FastAPI dependency — call it directly with ``async with``.

    Raises:
        RuntimeError: If the engine has not been initialised (the lifespan did not
            run; in tests, inject a session provider instead of relying on this).
    """
    if _session_factory is None:
        raise RuntimeError(
            "Database not initialised — standalone_session needs the lifespan-managed engine. "
            "In tests, pass a session provider that yields the test session."
        )
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def transactional[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Commit the request's DB session on success, roll back on failure.

    `get_db` only manages session lifecycle, not transaction boundaries — a
    handler that calls `session.flush()` without committing will see its
    INSERTs rolled back when the session closes. Apply this decorator to
    mutating endpoints (POST/PATCH/DELETE) so their work actually persists;
    read-only handlers don't need it and shouldn't pay for an empty commit.

    The wrapped handler must accept a keyword argument named ``db`` typed as
    `AsyncSession` — the same name used by the `DbSession` dependency alias.

    Apply *inside* `@router.<verb>(...)` so FastAPI registers the wrapped
    function:

        @router.post("/users")
        @transactional
        async def create_user_endpoint(..., db: DbSession) -> ...:
            ...
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        session = kwargs.get("db")
        if not isinstance(session, AsyncSession):
            msg = "@transactional requires a 'db' kwarg of type AsyncSession"
            raise TypeError(msg)
        try:
            result = await func(*args, **kwargs)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return result

    return wrapper
