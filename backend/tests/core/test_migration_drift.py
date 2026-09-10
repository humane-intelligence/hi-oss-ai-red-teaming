"""The migrated schema must match the models — a forgotten migration fails here.

`make checkmigrations` only guards linearity (single head) and the ERD dump only
guards the committed diagram; neither notices a model change with no migration.
This compares the live (fully migrated) test schema against `SQLModel.metadata`
with the same knobs `alembic/env.py` configures, so whatever autogenerate would
emit shows up as a failing diff here.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection as SyncConnection
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel

import app.models  # noqa: F401 — populates SQLModel.metadata, same as alembic/env.py

pytestmark = pytest.mark.integration


async def test_migrated_schema_matches_models(db_engine: AsyncEngine) -> None:
    def _diff(sync_conn: SyncConnection) -> list[object]:
        ctx = MigrationContext.configure(
            sync_conn,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return compare_metadata(ctx, SQLModel.metadata)

    async with db_engine.connect() as conn:
        diff = await conn.run_sync(_diff)

    assert diff == [], f"model ↔ migration drift (what autogenerate would emit): {diff}"
