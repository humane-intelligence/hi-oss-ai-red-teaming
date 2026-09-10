"""Unit tests for `scripts.dump_erd`.

`render()` is pure introspection of the live `SQLModel.metadata`, so the
high-level assertions run against the real schema. The cardinality arms use
synthetic tables instead, so they don't depend on which live FKs happen to be
nullable.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlmodel import SQLModel

from scripts import dump_erd


@pytest.mark.unit
def test_render_is_deterministic() -> None:
    assert dump_erd.render() == dump_erd.render()


@pytest.mark.unit
def test_render_emits_fenced_mermaid_er_diagram() -> None:
    rendered = dump_erd.render()

    assert "```mermaid\nerDiagram\n" in rendered
    assert rendered.endswith("```\n")


@pytest.mark.unit
def test_render_includes_every_table_as_entity() -> None:
    rendered = dump_erd.render()

    for table in SQLModel.metadata.sorted_tables:
        assert f"    {table.name} {{" in rendered


@pytest.mark.unit
def test_column_rows_put_name_before_postgres_type() -> None:
    rendered = dump_erd.render()

    # Compiled for Postgres (UUID, not the generic CHAR(32) fallback), spaces
    # collapsed so Mermaid parses the type as one token.
    assert "        id UUID PK\n" in rendered
    assert "        created_at TIMESTAMP_WITH_TIME_ZONE\n" in rendered


def _child_table(*, nullable: bool) -> Table:
    metadata = MetaData()
    Table("parents", metadata, Column("id", Integer, primary_key=True))
    return Table(
        "children",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parents.id"), nullable=nullable),
    )


@pytest.mark.unit
def test_required_fk_renders_exactly_one_parent() -> None:
    table = _child_table(nullable=False)

    assert dump_erd._relationships(table) == ['    parents ||--o{ children : "parent_id"']


@pytest.mark.unit
def test_nullable_fk_renders_zero_or_one_parent() -> None:
    table = _child_table(nullable=True)

    assert dump_erd._relationships(table) == ['    parents |o--o{ children : "parent_id"']


@pytest.mark.unit
def test_main_writes_rendered_diagram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "erd.md"
    monkeypatch.setattr(dump_erd, "OUTPUT", output)

    dump_erd.main()

    assert output.read_text(encoding="utf-8") == dump_erd.render()


@pytest.mark.unit
def test_main_skips_write_on_identical_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "erd.md"
    output.write_text(dump_erd.render(), encoding="utf-8")
    os.utime(output, (0, 0))  # sentinel mtime — a rewrite would bump it
    monkeypatch.setattr(dump_erd, "OUTPUT", output)

    dump_erd.main()

    assert output.stat().st_mtime == 0
