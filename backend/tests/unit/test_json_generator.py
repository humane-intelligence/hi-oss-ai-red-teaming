"""Unit tests for the generic declarative JSON generator (pure logic, no I/O)."""

import json
from datetime import UTC
from datetime import date
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import pytest

from app.core.csv_generator import Column
from app.core.json_generator import iter_json
from app.core.json_generator import json_row
from app.core.json_generator import to_json


class _Color(StrEnum):
    RED = "red"


_COLUMNS = [
    Column[dict]("Name", lambda r: r["name"]),
    Column[dict]("Score", lambda r: r["score"]),
]


@pytest.mark.unit
def test_to_json_emits_array_of_objects_keyed_by_header() -> None:
    rows = [{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}]

    assert json.loads(to_json(rows, _COLUMNS)) == [
        {"Name": "alpha", "Score": 1},
        {"Name": "beta", "Score": 2},
    ]


@pytest.mark.unit
def test_empty_rows_is_empty_array() -> None:
    assert json.loads(to_json([], _COLUMNS)) == []


@pytest.mark.unit
def test_types_coerced_to_json_safe() -> None:
    uid = UUID("12345678-1234-5678-1234-567812345678")
    cols = [
        Column[dict]("id", lambda r: r["id"]),
        Column[dict]("ts", lambda r: r["ts"]),
        Column[dict]("d", lambda r: r["d"]),
        Column[dict]("status", lambda r: r["status"]),
        Column[dict]("flag", lambda r: r["flag"]),
        Column[dict]("empty", lambda r: r["empty"]),
    ]
    row = {
        "id": uid,
        "ts": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "d": date(2026, 1, 2),
        "status": _Color.RED,
        "flag": True,
        "empty": None,
    }

    obj = json.loads(to_json([row], cols))[0]

    assert obj == {
        "id": str(uid),
        "ts": "2026-01-02T03:04:05+00:00",
        "d": "2026-01-02",
        "status": "red",
        "flag": True,
        "empty": None,
    }


@pytest.mark.unit
def test_nested_list_preserved_as_array() -> None:
    # The nested-message columns (engagement_report/transcript) return a raw list of dicts;
    # JSON must keep it a real nested array, not a stringified blob.
    cols = [Column[dict]("Messages", lambda r: r["messages"])]
    row = {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}

    obj = json.loads(to_json([row], cols))[0]

    assert obj == {"Messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}


@pytest.mark.unit
def test_json_uses_value_not_formatter() -> None:
    # `formatter` is CSV presentation only; JSON must serialize the raw `value`.
    cols = [Column[dict]("Amount", value=lambda r: r["amount"], formatter=lambda v: f"{v:.2f}")]

    obj = json.loads(to_json([{"amount": 3}], cols))[0]

    assert obj == {"Amount": 3}  # not "3.00"


@pytest.mark.unit
def test_iter_json_streams_incrementally() -> None:
    rows = [{"name": "a", "score": 1}, {"name": "b", "score": 2}]

    chunks = list(iter_json(rows, _COLUMNS))

    # Open bracket + one chunk per row + close bracket — never the whole document at once.
    assert chunks[0] == "["
    assert chunks[-1] == "]"
    assert "".join(chunks) == to_json(rows, _COLUMNS)
    assert json.loads("".join(chunks)) == [{"Name": "a", "Score": 1}, {"Name": "b", "Score": 2}]


@pytest.mark.unit
def test_json_row_is_one_object() -> None:
    assert json.loads(json_row({"name": "a", "score": 1}, _COLUMNS)) == {"Name": "a", "Score": 1}


@pytest.mark.unit
def test_unknown_leaf_falls_back_to_str() -> None:
    # Parity with csv_generator.format_cell: a non-JSON-native leaf (e.g. Decimal) is stringified,
    # never raised on — so a JSON export can't fail on a value its CSV twin would happily render.
    cols = [Column[dict]("Amount", lambda r: r["amount"])]

    obj = json.loads(to_json([{"amount": Decimal("3.50")}], cols))[0]

    assert obj == {"Amount": "3.50"}
