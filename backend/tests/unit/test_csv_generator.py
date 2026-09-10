"""Unit tests for the generic declarative CSV generator (pure logic, no I/O)."""

import csv
import io
from datetime import UTC
from datetime import date
from datetime import datetime
from decimal import Decimal

import pytest

from app.core.csv_generator import Column
from app.core.csv_generator import format_cell
from app.core.csv_generator import iter_csv
from app.core.csv_generator import to_csv

_COLUMNS = [
    Column[dict]("Name", lambda r: r["name"]),
    Column[dict]("Score", lambda r: r["score"]),
]


@pytest.mark.unit
def test_to_csv_emits_header_then_rows() -> None:
    rows = [{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}]

    parsed = list(csv.reader(io.StringIO(to_csv(rows, _COLUMNS))))

    assert parsed == [["Name", "Score"], ["alpha", "1"], ["beta", "2"]]


@pytest.mark.unit
def test_header_only_when_no_rows() -> None:
    parsed = list(csv.reader(io.StringIO(to_csv([], _COLUMNS))))

    assert parsed == [["Name", "Score"]]


@pytest.mark.unit
def test_special_characters_are_quoted_and_round_trip() -> None:
    # Commas, quotes, and newlines inside a cell must survive a parse round-trip.
    rows = [{"name": 'a,b "c"\nd', "score": 0}]

    parsed = list(csv.reader(io.StringIO(to_csv(rows, _COLUMNS))))

    assert parsed[1] == ['a,b "c"\nd', "0"]


@pytest.mark.unit
def test_iter_csv_streams_header_first_then_one_chunk_per_row() -> None:
    rows = [{"name": "a", "score": 1}, {"name": "b", "score": 2}]

    chunks = list(iter_csv(rows, _COLUMNS))

    # One chunk for the header plus one per row — never the whole document at once.
    assert len(chunks) == len(rows) + 1
    assert chunks[0] == "Name,Score\r\n"
    assert "".join(chunks) == to_csv(rows, _COLUMNS)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05+00:00"),
        (date(2026, 1, 2), "2026-01-02"),  # plain date — not swallowed by the datetime branch
        (42, "42"),
        ("plain", "plain"),
    ],
)
def test_format_cell_renders_types_consistently(value: object, expected: str) -> None:
    assert format_cell(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize("value", ["=HYPERLINK()", "+1", "-cmd", "@SUM()"])
def test_format_cell_neutralizes_formula_injection_in_strings(value: str) -> None:
    # A string cell starting with a formula trigger is prefixed with `'` so a spreadsheet
    # treats it as text — attacker-controlled free text (flag reasons, review notes) can't
    # execute in Excel/Sheets.
    assert format_cell(value) == f"'{value}"


@pytest.mark.unit
@pytest.mark.parametrize("prefix", [" ", "\t", "\r", "  \t"])
def test_format_cell_neutralizes_formula_after_leading_whitespace(prefix: str) -> None:
    # Spreadsheet importers strip leading whitespace before evaluating, so " =CMD()" is still a
    # formula — the guard checks the first non-whitespace character.
    value = f"{prefix}=HYPERLINK()"
    assert format_cell(value) == f"'{value}"


@pytest.mark.unit
def test_format_cell_leaves_safe_strings_and_numerics_untouched() -> None:
    assert format_cell("safe text") == "safe text"
    assert format_cell("  leading spaces then text") == "  leading spaces then text"  # no trigger
    assert format_cell("\t") == "\t"  # bare whitespace is not a formula
    # Negative numbers reach format_cell as int/float/Decimal — not strings — so they are
    # never prefixed (a `'-5` would corrupt the value).
    assert format_cell(-5) == "-5"
    assert format_cell(-5.5) == "-5.5"
    assert format_cell(Decimal(-3)) == "-3"


@pytest.mark.unit
def test_per_column_formatter_overrides_default() -> None:
    # A column's own formatter takes over presentation (e.g. fixed-precision money),
    # bypassing format_cell's str() (which would print Decimal("3.5") as "3.5").
    columns = [Column[dict]("Amount", value=lambda r: r["amount"], formatter=lambda v: f"{v:.2f}")]

    parsed = list(csv.reader(io.StringIO(to_csv([{"amount": Decimal("3.5")}], columns))))

    assert parsed == [["Amount"], ["3.50"]]
