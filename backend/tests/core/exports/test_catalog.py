"""Unit tests for the CSV export catalog (registry lookups — pure, no I/O)."""

import pytest

from app.core.auth.roles import Permission
from app.core.exports.catalog import get_export
from app.core.exports.catalog import list_exports
from app.core.exports.templates.conversation_groups import CONVERSATION_GROUPS_EXPORT
from app.core.exports.templates.flags import FLAGS_EXPORT


@pytest.mark.unit
def test_list_exports_is_sorted_and_covers_known_keys() -> None:
    keys = [export.key for export in list_exports()]

    assert keys == sorted(keys)
    assert {
        "flags",
        "conversations",
        "conversation-groups",
        "engagement_report",
        "reviews",
        "transcript",
    } <= set(keys)


@pytest.mark.unit
def test_conversation_groups_export_supports_the_scenario_filter() -> None:
    # A group targets exactly one scenario, so the dimension the conversations export honors
    # applies one level up; undeclared, the request is rejected as unsupported (400).
    assert "scenario_id" in CONVERSATION_GROUPS_EXPORT.supported_filters


@pytest.mark.unit
def test_get_export_returns_entry_or_none() -> None:
    assert get_export("flags") is FLAGS_EXPORT
    assert get_export("does-not-exist") is None


@pytest.mark.unit
def test_flags_export_requires_flags_read_and_has_columns() -> None:
    assert FLAGS_EXPORT.permission == Permission.FLAGS_READ
    assert FLAGS_EXPORT.columns
    assert FLAGS_EXPORT.columns[0].header == "Flag ID"
