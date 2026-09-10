"""Dump the SQLModel schema to a Mermaid ER diagram in ``docs/erd.md``.

Pure introspection of ``SQLModel.metadata`` — importing ``app.models`` registers
every table, so no live database (or running app) is needed. Mirrors
``scripts/dump_openapi.py``: deterministic output, skip-write on identical
content so the pre-push hook does not see a spurious change.
"""

from pathlib import Path

from sqlalchemy import Column
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

import app.models  # noqa: F401 — populates SQLModel.metadata with every table

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "erd.md"

_PG = postgresql.dialect()

HEADER = """# Database schema (ERD)

Auto-generated from the SQLModel metadata by `make erddump` — **do not edit by
hand**. Regenerate after any model change and commit the result in the same diff
(the pre-push hook does this for you; CI fails on drift).

```mermaid
erDiagram
"""

FOOTER = "```\n"


def _render_type(column: Column) -> str:
    """Mermaid-safe Postgres type token for one column.

    Compiled against the Postgres dialect so the diagram shows the real column
    types (``UUID``, ``TIMESTAMP WITH TIME ZONE``, the named enum, …) rather than
    SQLAlchemy's dialect-agnostic fallbacks (``CHAR(32)``, ``DATETIME``). Spaces
    are collapsed because Mermaid attribute types cannot contain whitespace,
    e.g. ``TIMESTAMP WITH TIME ZONE`` -> ``TIMESTAMP_WITH_TIME_ZONE``.
    """
    return column.type.compile(dialect=_PG).replace(" ", "_")


def _column_line(column: Column) -> str:
    # Mermaid renders attribute rows as two unlabeled columns in token order, so
    # emitting `name type` puts the column name first; PK/FK keys still parse as
    # the trailing tokens.
    keys = []
    if column.primary_key:
        keys.append("PK")
    if column.foreign_keys:
        keys.append("FK")
    suffix = f" {','.join(keys)}" if keys else ""
    return f"        {column.name} {_render_type(column)}{suffix}"


def _entity(table: Table) -> str:
    lines = [f"    {table.name} {{"]
    lines += [_column_line(c) for c in table.columns]
    lines.append("    }")
    return "\n".join(lines)


def _relationships(table: Table) -> list[str]:
    """One Mermaid relationship per FK constraint, sorted for stable output.

    Cardinality: parent is exactly-one (``||``) for a non-nullable FK, or
    zero-or-one (``|o``) when any local column is nullable; the child is always
    zero-or-many (``o{``). The label is the local column list.
    """
    rels = []
    for fk in table.foreign_key_constraints:
        local_cols = [el.parent for el in fk.elements]
        parent = fk.referred_table.name
        nullable = any(c.nullable for c in local_cols)
        parent_card = "|o" if nullable else "||"
        label = ",".join(c.name for c in local_cols)
        line = f'    {parent} {parent_card}--o{{ {table.name} : "{label}"'
        rels.append((parent, label, line))
    return [line for *_, line in sorted(rels)]


def render() -> str:
    tables = SQLModel.metadata.sorted_tables
    entities = "\n".join(_entity(t) for t in tables)
    relationships = "\n".join(line for t in tables for line in _relationships(t))
    body = entities + ("\n\n" + relationships if relationships else "")
    return HEADER + body + "\n" + FOOTER


def main() -> None:
    rendered = render()
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == rendered:
        return
    OUTPUT.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
