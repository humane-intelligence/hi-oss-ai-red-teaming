"""Behavior tests for the modality-array data migration (revision `4849687935f4`).

The migration only runs once at test-session setup, over empty tables, so neither
direction's `CASE` ever touches a row there. Both statements are exercised here
directly: the pre-migration columns are restored inside the test transaction (and
rolled back with it), seeded one row per cell, and run through the real SQL.
"""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import TextClause
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = "4849687935f4"

# (supports_image_input, modality) -> the row's alias, one per upgrade cell.
_CELLS = {
    "img-in-img-out": (True, "text_to_image"),
    "img-in-text-out": (True, "text_to_text"),
    "text-in-img-out": (False, "text_to_image"),
    "text-in-text-out": (False, "text_to_text"),
}


def _statement(name: str) -> TextClause:
    """Load a backfill statement from the migration via Alembic (no fragile file-path import)."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return getattr(script.get_revision(_REVISION).module, name)


async def _restore_pre_migration_columns(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TYPE modelmodality AS ENUM ('text_to_text', 'text_to_image')"))
    await db_session.execute(
        text(
            "ALTER TABLE ai_models "
            "ADD COLUMN modality modelmodality NOT NULL DEFAULT 'text_to_text', "
            "ADD COLUMN supports_image_input BOOLEAN NOT NULL DEFAULT false"
        )
    )


async def _seed_cells(db_session: AsyncSession) -> None:
    for alias, (supports_image_input, modality) in _CELLS.items():
        await db_session.execute(
            text(
                # CAST(...) rather than `::modelmodality`: the `::` right after a bind
                # parameter swallows the parameter name in SQLAlchemy's `text()` parser.
                "INSERT INTO ai_models (id, name, model_alias, provider, provider_model_id, "
                "modality, supports_image_input) "
                "VALUES (gen_random_uuid(), :alias, :alias, 'openai', 'x', "
                "CAST(:modality AS modelmodality), :supports_image_input)"
            ),
            {"alias": alias, "modality": modality, "supports_image_input": supports_image_input},
        )


async def _modalities_by_alias(db_session: AsyncSession) -> dict[str, tuple[list[str], list[str]]]:
    rows = (
        await db_session.execute(
            text("SELECT model_alias, input_modalities, output_modalities FROM ai_models ORDER BY model_alias")
        )
    ).all()
    return {
        alias: (list(input_modalities), list(output_modalities)) for alias, input_modalities, output_modalities in rows
    }


async def test_upgrade_backfill_maps_every_cell(db_session: AsyncSession) -> None:
    await _restore_pre_migration_columns(db_session)
    await _seed_cells(db_session)

    await db_session.execute(_statement("_UPGRADE_BACKFILL"))

    assert await _modalities_by_alias(db_session) == {
        "img-in-img-out": (["text", "image"], ["image"]),
        "img-in-text-out": (["text", "image"], ["text"]),
        "text-in-img-out": (["text"], ["image"]),
        "text-in-text-out": (["text"], ["text"]),
    }


async def test_downgrade_backfill_collapses_dual_output_to_text(db_session: AsyncSession) -> None:
    await _restore_pre_migration_columns(db_session)
    await db_session.execute(
        text(
            "INSERT INTO ai_models (id, name, model_alias, provider, provider_model_id, "
            "input_modalities, output_modalities) "
            "VALUES (gen_random_uuid(), 'dual', 'dual', 'openai', 'x', "
            "ARRAY['text', 'image'], ARRAY['text', 'image'])"
        )
    )

    await db_session.execute(_statement("_DOWNGRADE_BACKFILL"))

    row = (
        await db_session.execute(
            text("SELECT supports_image_input, modality::text FROM ai_models WHERE model_alias = 'dual'")
        )
    ).one()
    # The old enum held one output shape, so `image` is dropped — the documented loss.
    assert row == (True, "text_to_text")


async def test_upgrade_and_downgrade_round_trip_is_stable(db_session: AsyncSession) -> None:
    await _restore_pre_migration_columns(db_session)
    await _seed_cells(db_session)
    await db_session.execute(_statement("_UPGRADE_BACKFILL"))
    before = await _modalities_by_alias(db_session)

    await db_session.execute(_statement("_DOWNGRADE_BACKFILL"))
    await db_session.execute(_statement("_UPGRADE_BACKFILL"))

    # Every cell the old shape could represent survives the round trip unchanged.
    assert await _modalities_by_alias(db_session) == before
