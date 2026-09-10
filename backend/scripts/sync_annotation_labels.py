"""Synchronize the curated annotation labels into the database.

Deploy step — run after ``alembic upgrade head`` in *every* environment (no ``local`` guard,
unlike `scripts.seed_local`). Idempotent: re-running upserts the labels defined in
`app/core/annotations/label_catalog.py`, so an added or reworded entry applies on the next
deploy. A **removed** entry does not — the upsert has no retire path, so its row stays live
and has to be tombstoned by hand.

**Fatal by design** when a curated wording is already held by another live row — a label an
annotator typed, or a curated row under a key the catalog no longer names. Nothing here can pick
a winner: which row survives, and where the annotations already pointing at it go, is a data
decision. Retire or rename the offending row by hand (SQL — there is no write route), or change
the catalog wording, then re-run. Under ``deploy.sh`` this aborts the deploy (``set -e``) and the
old stack keeps serving; see ``deploy/README.md`` for the remedy.

Invoked via ``make syncannotationlabels`` (or ``python -m scripts.sync_annotation_labels``).
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every table, so a flush can resolve cross-module FKs
from app.core.annotations.services.annotation_labels import sync_annotation_labels
from app.core.config import get_settings
from app.core.database import build_engine
from app.core.logging import configure_logging
from app.core.logging import get_logger

logger = get_logger(__name__)


async def sync() -> None:
    """Open a session, upsert the curated annotation labels, and commit."""
    settings = get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            count = await sync_annotation_labels(session)
            await session.commit()
        logger.info("sync_annotation_labels.done", environment=settings.environment, count=count)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync())
