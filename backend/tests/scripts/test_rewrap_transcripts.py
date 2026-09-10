"""Tests for the `scripts.rewrap_transcripts` operator entrypoint.

The sweep itself is exercised in `tests/core/conversations/services/test_rewrap.py`. What matters here
is the wrapper an operator actually types: that the real run sweeps (a run that quietly degenerated
into a second dry run would report "nothing left" and move nothing), that `--dry-run` writes nothing,
and that the exit code says what the rotation runbook claims it says.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.conversations.services.rewrap import RewrapResult
from scripts.rewrap_transcripts import exit_code
from scripts.rewrap_transcripts import rewrap_transcripts

pytestmark = pytest.mark.integration


def _counts(value: int):
    """A `stale_message_count` stub — the script asks once, after the work it does (or does not) do."""

    async def _count(_session: AsyncSession, _settings: object) -> int:
        return value

    return _count


async def test_the_real_run_actually_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    # The failure this guards against is the sweep call going missing: everything else still reports
    # plausible numbers and exits zero, so the rotation looks finished while nothing moved.
    swept = False

    async def _sweep(*_args: object, **kwargs: object) -> RewrapResult:
        nonlocal swept
        swept = True
        assert kwargs["chunk_size"] == 25, "the operator's chunk size has to reach the sweep"
        return RewrapResult(moved=3, unreadable=0, skipped=0)

    monkeypatch.setattr("scripts.rewrap_transcripts.stale_message_count", _counts(0))
    monkeypatch.setattr("scripts.rewrap_transcripts.rewrap_message_content", _sweep)

    assert await rewrap_transcripts(chunk_size=25) == (0, 0)
    assert swept, "a run that does not sweep is a dry run wearing the wrong name"


async def test_dry_run_reports_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    swept = False

    async def _sweep(*_args: object, **_kwargs: object) -> RewrapResult:
        nonlocal swept
        swept = True
        return RewrapResult(moved=0, unreadable=0, skipped=0)

    monkeypatch.setattr("scripts.rewrap_transcripts.stale_message_count", _counts(7))
    monkeypatch.setattr("scripts.rewrap_transcripts.rewrap_message_content", _sweep)

    assert await rewrap_transcripts(dry_run=True) == (7, 0)
    assert not swept, "--dry-run must not move any row"


async def test_rows_no_key_opens_are_reported_but_do_not_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sweep(*_args: object, **_kwargs: object) -> RewrapResult:
        return RewrapResult(moved=5, unreadable=2, skipped=0)

    monkeypatch.setattr("scripts.rewrap_transcripts.stale_message_count", _counts(2))
    monkeypatch.setattr("scripts.rewrap_transcripts.rewrap_message_content", _sweep)

    assert await rewrap_transcripts() == (2, 2)


@pytest.mark.parametrize(
    ("pending", "unreadable", "expected"),
    [
        (0, 0, 0),  # done
        (3, 0, 1),  # work a re-run can still do — stop the deploy step
        (2, 2, 0),  # nothing left that any run could move
        (5, 2, 1),  # three movable rows hide behind two unmovable ones
    ],
)
def test_exit_code_gates_on_work_a_rerun_could_do(pending: int, unreadable: int, expected: int) -> None:
    # Gating on `pending` alone would fail every rotation for ever once a single unmovable row exists,
    # which teaches the operator to ignore the code.
    assert exit_code(pending, unreadable) == expected
