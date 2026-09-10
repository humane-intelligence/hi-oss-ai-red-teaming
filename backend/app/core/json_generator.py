"""Generic, declarative JSON generation — the JSON sibling of `csv_generator`.

Reuses the same `Column` specs an export already declares: each row becomes a JSON object
keyed by `Column.header`, whose value is the raw `Column.value(row)` (NOT the `formatter` —
that is CSV presentation). So a template's `fetch` + `columns` render to JSON with no change.

Values are serialized with `json.dumps` under a `default` hook that maps the platform's
non-native-JSON leaf types (`datetime`/`date` → ISO-8601, `UUID` → str, `Enum` → its value).
`StrEnum` / `IntEnum` members are already `str`/`int` subclasses, so they serialize natively.
Nested lists/dicts (e.g. a transcript's message array) pass straight through as real JSON
arrays/objects — unlike CSV, which flattens them to a string.

Streaming-first: `iter_json` frames the array (`[`, comma-joined objects, `]`) one row-object
at a time, so only a single row is held in memory. The export path
(`exports.generation.stream_export`) frames the array itself across a multi-evaluation fetch
using `json_row`; `to_json` materializes the whole document for tests and eager callers.

There is no formula-injection concern here (that is a spreadsheet-only issue) and no BOM.
"""

import json
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from datetime import date
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from app.core.csv_generator import Column


def _json_default(value: Any) -> Any:
    """Serialize the leaf types `json` can't handle natively — the platform's UTC/UUID/enum shapes.

    `datetime`/`date` → ISO-8601 (the UTC contract); `UUID` → str; a plain `Enum` → its value.
    `StrEnum`/`IntEnum` never reach here (they are `str`/`int` subclasses `json` serializes
    directly). Anything else falls back to `str()` — parity with the CSV generator's `format_cell`,
    so a `format=json` export never fails on a column value that its CSV twin would happily render
    (e.g. a `Decimal`); the value is stringified rather than dropped.
    """
    if isinstance(value, (datetime, date)):  # datetime is a date subclass; both take isoformat()
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


def json_row[T](row: T, columns: Sequence[Column[T]]) -> str:
    """Render one row as a single compact JSON object (no array framing, no trailing comma).

    The per-row primitive the streaming export path builds on — no cross-row state, so it's
    safe to call one row at a time. Keys are column headers; values are the raw `Column.value`.
    """
    obj = {column.header: column.value(row) for column in columns}
    return json.dumps(obj, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def iter_json[T](rows: Iterable[T], columns: Sequence[Column[T]]) -> Iterator[str]:
    """Yield a JSON array one chunk at a time: `[`, each row-object (comma-prefixed after the first), `]`.

    Only one row-object is materialized at a time, so an eager caller still streams. Empty
    input yields `[` then `]` → `[]`.
    """
    yield "["
    first = True
    for row in rows:
        yield json_row(row, columns) if first else "," + json_row(row, columns)
        first = False
    yield "]"


def to_json[T](rows: Iterable[T], columns: Sequence[Column[T]]) -> str:
    """Materialize the whole JSON array as one string — for tests and eager, all-or-nothing callers.

    Raises before returning anything if a row fails (like `to_csv`), so there's no partial output.
    """
    return "".join(iter_json(rows, columns))
