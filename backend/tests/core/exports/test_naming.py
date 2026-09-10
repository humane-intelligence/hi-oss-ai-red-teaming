"""Human-readable export names — download filename + notification/email label."""

from datetime import UTC
from datetime import datetime

import pytest

from app.core.exports.naming import report_filename
from app.core.exports.naming import report_label

_TS = datetime(2026, 7, 24, 13, 9, tzinfo=UTC)


@pytest.mark.unit
def test_report_filename_is_human_slug_dated() -> None:
    assert (
        report_filename(scope_title="Demo Evaluation", template="flags", extension="csv", created_at=_TS)
        == "demo-evaluation-flags-20260724-1309.csv"
    )


@pytest.mark.unit
def test_report_filename_falls_back_when_no_title() -> None:
    assert (
        report_filename(scope_title=None, template="flags", extension="json", created_at=_TS)
        == "export-flags-20260724-1309.json"
    )


@pytest.mark.unit
def test_report_filename_slugifies_messy_title() -> None:
    # Punctuation / spaces collapse to single hyphens; no raw UUID, no stray characters.
    assert (
        report_filename(scope_title="Q3 // Safety!! Review", template="conversations", extension="csv", created_at=_TS)
        == "q3-safety-review-conversations-20260724-1309.csv"
    )


@pytest.mark.unit
def test_report_label_is_human_readable_with_timestamp() -> None:
    label = report_label(scope_title="Demo Evaluation", template="flags", export_format="csv", created_at=_TS)
    assert label.startswith("Demo Evaluation - ")  # single hyphen separator, not an em dash
    assert "—" not in label
    assert "Report (CSV) · 2026-07-24 13:09 UTC" in label  # "Report" appended after the template name
    assert "flags.csv" not in label  # no technical filename / uuid leaking into the human label


@pytest.mark.unit
def test_report_label_collapses_control_chars_in_title() -> None:
    # A title with CR/LF must not survive into the label — it flows into an email Subject header,
    # which rejects newlines (would otherwise fail the send). Whitespace collapses to one space.
    label = report_label(
        scope_title="Evil\r\nBcc: x@y.z\tReport", template="flags", export_format="csv", created_at=_TS
    )
    assert "\n" not in label
    assert "\r" not in label
    assert "\t" not in label
    assert label.startswith("Evil Bcc: x@y.z Report - ")


@pytest.mark.unit
def test_report_label_without_title_uses_generic_prefix() -> None:
    assert report_label(scope_title=None, template="flags", export_format="json", created_at=_TS).startswith(
        "Your export - "
    )
