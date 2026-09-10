"""Generic, declarative CSV generation — define columns once, stream any rows out.

Separates *what* to emit (a `Column` spec: a header, how to pull the cell value from a row,
and optionally how to format it) from *how* to write it (RFC 4180 quoting, default value
formatting, streaming). A new export is a new column list, not an edit to this module — so
the writer never needs tweaking per data shape. Rows are arbitrary objects or dicts; each
column derives its value via a callable, so nothing here is coupled to a record type.

Streaming-first primitives: `csv_header` + `format_csv_row` render the header and one row at
a time, holding a single row in memory. The export path (`exports.generation.stream_export`)
builds on those to stream a whole export into `storage.save` in the worker — never a client
`StreamingResponse`. `iter_csv` / `to_csv` materialize the document and are used by tests and
any eager caller.

**Failure mode.** If a `value`/`formatter` raises mid-stream, the already-yielded rows are
lost. On the export path the worker treats this as a job failure — it deletes the partial
stored file and marks the job `failed`, so a truncated file is never served. For an eager,
all-or-nothing build use `to_csv` (it raises before returning anything).

**Excel / encoding.** This module emits no byte-order mark. For Excel to render diacritics
correctly, prepend a UTF-8 BOM at the call site (e.g. yield ``"﻿"`` before the stream,
or write it to the response) — otherwise non-ASCII shows as mojibake.
"""

import csv
import io
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Column[T]:
    """One CSV column: a header, how to derive the cell value, and optionally how to format it.

    `value` pulls the raw cell from a row. `formatter`, when given, renders that raw value to
    a string — pass one to keep presentation out of extraction (e.g. money via `Decimal`, a
    custom date format, an id→name lookup). When omitted the shared `format_cell` is used.
    """

    header: str
    value: Callable[[T], Any]
    formatter: Callable[[Any], str] | None = None


# Characters that make a spreadsheet (Excel/Sheets) treat a cell as a formula.
_FORMULA_TRIGGERS = frozenset("=+-@")


def _neutralize_formula(text: str) -> str:
    """Defuse CSV formula injection by prefixing a leading `'` to a formula-triggering cell.

    Applied only to **string** cells — attacker-controlled free text (flag reasons/comments,
    review notes) that lands in a client-facing deliverable. Numerics reach `format_cell` as
    `int`/`float`/`Decimal`, so a negative number like ``-5`` is never touched. The `'` is the
    spreadsheet convention for "treat as text"; plain CSV consumers see the literal prefix.

    The trigger is checked against the first *non-whitespace* character, because spreadsheet
    importers strip leading whitespace before evaluating — so ``" =CMD()"`` (leading space, tab,
    CR) is still a formula and must be neutralised.
    """
    stripped = text.lstrip()
    if stripped and stripped[0] in _FORMULA_TRIGGERS:
        return "'" + text
    return text


def format_cell(value: Any) -> str:
    """Render one cell consistently so call sites don't reformat per export.

    `None` becomes an empty cell; `datetime`/`date` serialize as ISO-8601 (the platform's UTC
    contract); `bool` as `true`/`false`; a `str` is guarded against spreadsheet formula
    injection (`_neutralize_formula`); everything else via `str`. `datetime` is checked before
    `date` because it subclasses it. Numerics fall through to `str()`, which can emit scientific
    notation for very large/small floats and binary-rounding artifacts for money — for precise
    numerics use `Decimal` upstream or a per-column `formatter`.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _neutralize_formula(value)
    return str(value)


def csv_header[T](columns: Sequence[Column[T]]) -> str:
    """Render the header row (column names) as one CSV line.

    Split out so a multi-batch stream (e.g. a whole-group export that fetches one
    evaluation's rows at a time) can emit the header once up front and then append rows via
    `format_csv_row`, without re-emitting headers.
    """
    buffer = io.StringIO()
    csv.writer(buffer).writerow([column.header for column in columns])
    return buffer.getvalue()


def format_csv_row[T](row: T, columns: Sequence[Column[T]]) -> str:
    """Render one row as a single CSV line (RFC 4180 quoting), no header.

    The per-row primitive both the batch iterator and the async export stream build on —
    quoting is per-row with no cross-row state, so it's safe to call one row at a time.
    """
    buffer = io.StringIO()
    csv.writer(buffer).writerow([(column.formatter or format_cell)(column.value(row)) for column in columns])
    return buffer.getvalue()


def iter_csv_rows[T](rows: Iterable[T], columns: Sequence[Column[T]]) -> Iterator[str]:
    """Yield one CSV row-string per row (NO header), for streaming a batch of rows.

    Quoting is per-row (no cross-row state), so this is safe to call repeatedly for
    successive batches under one header. Only one row is buffered at a time.
    """
    for row in rows:
        yield format_csv_row(row, columns)


def iter_csv[T](rows: Iterable[T], columns: Sequence[Column[T]]) -> Iterator[str]:
    """Yield the CSV one row-string at a time (header first) over an in-memory row iterable.

    Uses the stdlib writer so commas, quotes, and newlines inside a cell are quoted per
    RFC 4180. Backs `to_csv`; the export path streams via `stream_export` instead (async,
    over a paginated fetch).
    """
    yield csv_header(columns)
    yield from iter_csv_rows(rows, columns)


def to_csv[T](rows: Iterable[T], columns: Sequence[Column[T]]) -> str:
    """Materialize the whole CSV as one string — for tests and eager, all-or-nothing callers.

    Raises before returning anything if a row fails, so there's no partial output (unlike the
    incremental `stream_export` used on the export path).
    """
    return "".join(iter_csv(rows, columns))
