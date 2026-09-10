"""Synchronize the curated data licenses into the database.

Deploy step — run after ``alembic upgrade head`` in *every* environment (no ``local`` guard,
unlike `scripts.seed_local`). Idempotent: re-running upserts the curated licenses defined in
`app/core/licenses/catalog.py` (metadata + full text), so a catalog change applies on the next
deploy. Invoked via ``make synclicenses`` (or ``python -m scripts.sync_licenses``).
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every table, so a flush can resolve cross-module FKs
from app.core.config import get_settings
from app.core.database import build_engine
from app.core.licenses.service import sync_licenses
from app.core.logging import configure_logging
from app.core.logging import get_logger

logger = get_logger(__name__)


async def sync() -> None:
    """Open a session, upsert the curated licenses, and commit."""
    settings = get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            count = await sync_licenses(session)
            await session.commit()
        logger.info("sync_licenses.done", environment=settings.environment, count=count)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync())
