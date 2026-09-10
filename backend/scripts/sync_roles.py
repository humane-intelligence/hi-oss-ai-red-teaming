"""Synchronize the canonical system roles into the database.

Deploy step — run after ``alembic upgrade head`` in *every* environment (no
``local`` guard, unlike `scripts.seed_local`). Idempotent: re-running upserts
the roles defined in `app/core/auth/roles.py`, so a mapping change applies on
the next deploy. Invoked via ``make syncroles`` (or ``python -m scripts.sync_roles``).
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every table, so a flush can resolve cross-module FKs
from app.core.auth.services.roles import sync_system_roles
from app.core.config import get_settings
from app.core.database import build_engine
from app.core.logging import configure_logging
from app.core.logging import get_logger

logger = get_logger(__name__)


async def sync_roles() -> None:
    """Open a session, upsert the canonical roles, and commit."""
    settings = get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await sync_system_roles(session)
            await session.commit()
        logger.info("sync_roles.done", environment=settings.environment)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_roles())
