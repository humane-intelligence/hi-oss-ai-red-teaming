"""Schema conventions every table model must obey — introspected over `SQLModel.metadata`.

A new model is covered the moment it is imported into `app/models.py` (which the
Alembic env requires anyway); exemptions are explicit sets below, with reasons.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import DateTime
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Connection as SyncConnection
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel

import app.models  # noqa: F401 — populates SQLModel.metadata
from app.core.base_model import BaseModel

_PLATFORM_COLUMNS = {"id", "created_at", "updated_at", "deleted_at"}

# (table, column) FKs that shipped without an index — inherited debt, not license.
# A new FK must come indexed; fixing one of these means removing its row here.
_UNINDEXED_FK_RATCHET: set[tuple[str, str]] = {
    ("export_jobs", "evaluation_group_id"),
    ("export_jobs", "evaluation_id"),
    ("invitations", "invited_by_user_id"),
    ("object_role_assignments", "role_id"),
    ("reviews", "assigned_by_id"),
    ("task_completions", "task_id"),
    ("user_roles", "role_id"),
}


def _base_model_table_names() -> list[str]:
    found: list[str] = []
    stack = list(BaseModel.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        name = getattr(cls, "__tablename__", None)
        if isinstance(name, str) and name in SQLModel.metadata.tables:
            found.append(name)
    return found


@pytest.mark.unit
def test_base_model_tables_carry_platform_columns() -> None:
    violations = [
        f"{name}: missing {sorted(_PLATFORM_COLUMNS - columns)}"
        for name in _base_model_table_names()
        if not (columns := set(SQLModel.metadata.tables[name].columns.keys())) >= _PLATFORM_COLUMNS
    ]

    assert not violations, violations


@pytest.mark.unit
def test_datetime_columns_are_timezone_aware() -> None:
    # CLAUDE.md rule: all datetimes are UTC, DB columns are DateTime(timezone=True).
    violations = [
        f"{table.name}.{column.name}"
        for table in SQLModel.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]

    assert not violations, violations


@pytest.mark.integration
async def test_foreign_key_delete_rules_match_models(db_engine: AsyncEngine) -> None:
    """Model-declared `ondelete` must match the migrated DDL — autogenerate is blind here.

    A drifted rule (model says CASCADE, DB says NO ACTION) never surfaces in a diff
    and only bites on the rare hard-delete paths. A deliberately bare FK (both sides
    None → NO ACTION) is consistent and passes.
    """

    def _db_rules(sync_conn: SyncConnection) -> dict[tuple[str, str], str]:
        inspector = sa_inspect(sync_conn)
        return {
            (table, column): (fk.get("options") or {}).get("ondelete") or "NO ACTION"
            for table in inspector.get_table_names()
            for fk in inspector.get_foreign_keys(table)
            for column in fk["constrained_columns"]
        }

    async with db_engine.connect() as conn:
        db_rules = await conn.run_sync(_db_rules)

    violations = [
        f"{table.name}.{column.name}: model={fk.ondelete or 'NO ACTION'} db={db_rules.get((table.name, column.name))}"
        for table in SQLModel.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if (fk.ondelete or "NO ACTION").upper() != (db_rules.get((table.name, column.name)) or "NO ACTION").upper()
    ]

    assert not violations, violations


@pytest.mark.integration
async def test_foreign_key_columns_are_indexed(db_engine: AsyncEngine) -> None:
    """Every FK column leads an index (or the PK) in the migrated DDL.

    Postgres does not index FK columns automatically; an unindexed FK makes the
    parent-side lookup/delete a sequential scan. Leading position counts — a column
    that only trails in a composite index is unreachable for that scan. Inherited
    misses live in the ratchet below with a reason; new FKs must come indexed.
    """

    def _db_state(sync_conn: SyncConnection) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        inspector = sa_inspect(sync_conn)
        fk_columns = {
            (table, column)
            for table in inspector.get_table_names()
            for fk in inspector.get_foreign_keys(table)
            for column in fk["constrained_columns"]
        }
        leaders = set()
        for table in inspector.get_table_names():
            for index in inspector.get_indexes(table):
                if index["column_names"] and index["column_names"][0]:
                    leaders.add((table, index["column_names"][0]))
            # UNIQUE-constraint backing indexes don't surface via get_indexes on Postgres.
            for unique in inspector.get_unique_constraints(table):
                if unique["column_names"]:
                    leaders.add((table, unique["column_names"][0]))
            pk = inspector.get_pk_constraint(table)
            if pk["constrained_columns"]:
                leaders.add((table, pk["constrained_columns"][0]))
        return fk_columns, leaders

    async with db_engine.connect() as conn:
        fk_columns, leaders = await conn.run_sync(_db_state)

    violations = sorted(f"{t}.{c}" for t, c in fk_columns - leaders if (t, c) not in _UNINDEXED_FK_RATCHET)
    fixed = sorted(f"{t}.{c}" for t, c in _UNINDEXED_FK_RATCHET if (t, c) not in fk_columns - leaders)

    assert not violations, f"new unindexed FK columns: {violations}"
    assert not fixed, f"ratchet entries no longer needed — remove them: {fixed}"


# Entry points that open a session and write ORM rows. A flush resolves every FK on the table it
# touches, and those targets live in modules the entry point may not import itself — so each one
# has to pull in the registry. Verified in a subprocess because this test process imports
# `app.models` above, which would mask exactly the gap being checked (as it did for the
# `users.accepted_terms_id` FK, caught only by running `make seedlocal` by hand).
_ORM_ENTRY_POINTS = [
    "app.main",
    "app.workers.celery_app",
    "scripts.seed_local",
    "scripts.sync_roles",
    "scripts.sync_licenses",
    "scripts.sync_annotation_labels",
    "scripts.rewrap_transcripts",
]

_FK_PROBE = """
import importlib, sys
importlib.import_module(sys.argv[1])
from sqlmodel import SQLModel
unresolved = []
for table in SQLModel.metadata.tables.values():
    for fk in table.foreign_keys:
        try:
            fk.column
        except Exception:
            unresolved.append(str(fk.parent))
print(",".join(sorted(unresolved)))
"""


@pytest.mark.unit
@pytest.mark.parametrize("module", _ORM_ENTRY_POINTS)
def test_orm_entry_point_resolves_every_foreign_key(module: str) -> None:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
        [sys.executable, "-c", _FK_PROBE, module],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.stdout.strip() == "", (
        f"{module} leaves these FKs unresolvable: {result.stdout.strip()}. Add `import app.models` to it."
    )
