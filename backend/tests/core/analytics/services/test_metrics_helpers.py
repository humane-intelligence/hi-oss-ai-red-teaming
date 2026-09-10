"""Pure-logic coverage for the `metrics` service helpers that need no database.

The window rule decides which days a chart spans, and its interesting cases (a group whose declared
window has not started, data outside it, a years-old start date) are awkward to seed against a
database — they are exercised here so the integration suite only carries the representative ones.
The exploit predicate's shape is here for the opposite reason: it is a planner hint with no
observable behaviour, so the only thing that can guard it is the SQL it renders.
"""

from datetime import date
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.analytics.services.metrics import _daily_series
from app.core.analytics.services.metrics import _has_successful_exploit_review
from app.core.analytics.services.metrics import _timeline_window
from app.core.annotations.models import MessageFlag

pytestmark = pytest.mark.unit

TODAY = date(2026, 8, 6)


def days_ago(count: int) -> date:
    return TODAY - timedelta(days=count)


def days_ahead(count: int) -> date:
    return TODAY + timedelta(days=count)


@pytest.mark.parametrize(
    ("start_date", "end_date", "observed", "expected_first", "expected_last"),
    [
        pytest.param(days_ago(10), None, set(), days_ago(10), TODAY, id="running-event-no-data"),
        pytest.param(days_ago(10), None, {days_ago(3)}, days_ago(10), TODAY, id="data-inside-window"),
        pytest.param(days_ago(3), None, {days_ago(9)}, days_ago(9), TODAY, id="data-before-start"),
        pytest.param(days_ago(10), days_ago(5), {days_ago(2)}, days_ago(10), days_ago(2), id="data-after-end"),
        pytest.param(days_ago(10), days_ago(5), {days_ago(7)}, days_ago(10), days_ago(5), id="finished-event"),
        pytest.param(days_ago(800), None, set(), days_ago(365), TODAY, id="ancient-start-no-data"),
        pytest.param(days_ago(800), None, {days_ago(700)}, days_ago(700), TODAY, id="ancient-start-ancient-data"),
        pytest.param(days_ago(800), None, {days_ago(3)}, days_ago(365), TODAY, id="ancient-start-recent-data"),
        pytest.param(None, None, {days_ago(2), TODAY}, days_ago(2), TODAY, id="no-declared-dates"),
        # A draft may carry any ordered pair of dates, including year 1, where subtracting the
        # lead-in would overflow `date`.
        pytest.param(
            date.min, date.min + timedelta(days=1), set(), date.min, date.min + timedelta(days=1), id="year-one-draft"
        ),
        # The lead-in is capped against the axis's *own* end, so an `end_date` older than the data
        # cannot shrink the cap and leave the leading edge back at the declared start. Both arms of
        # that: a year-1 draft that did receive a submission, and the ordinary shape — a group
        # finished long ago with one late submission.
        pytest.param(
            date.min, date.min + timedelta(days=1), {TODAY}, days_ago(365), TODAY, id="year-one-draft-with-data"
        ),
        # 368, not 365: the cap is measured from the axis's end, which the late submission moved to
        # `days_ago(3)` — so the year of lead-in runs back from there, not from today.
        pytest.param(
            days_ago(800), days_ago(700), {days_ago(3)}, days_ago(368), days_ago(3), id="ancient-end-recent-data"
        ),
        pytest.param(days_ago(400), days_ago(370), set(), days_ago(400), days_ago(370), id="finished-before-the-clamp"),
    ],
)
def test_timeline_window_spans(
    start_date: date | None,
    end_date: date | None,
    observed: set[date],
    expected_first: date,
    expected_last: date,
) -> None:
    window = _timeline_window(start_date, end_date, observed, today=TODAY)

    assert window[0] == expected_first
    assert window[-1] == expected_last
    assert len(window) == (expected_last - expected_first).days + 1


def test_timeline_window_is_dense_and_ascending() -> None:
    window = _timeline_window(days_ago(4), None, {days_ago(2)}, today=TODAY)

    assert window == [days_ago(4), days_ago(3), days_ago(2), days_ago(1), TODAY]


def test_timeline_window_covers_every_observed_day() -> None:
    observed = {days_ago(400), days_ago(3), days_ahead(2)}

    window = _timeline_window(days_ago(10), days_ago(5), observed, today=TODAY)

    assert observed <= set(window)


def test_timeline_window_is_empty_without_a_start_date_or_data() -> None:
    assert _timeline_window(None, None, set(), today=TODAY) == []


def test_daily_series_zero_fills_the_window() -> None:
    counts = {days_ago(2): (4, 1), TODAY: (2, 0)}

    series = _daily_series(counts, [days_ago(2), days_ago(1), TODAY])

    assert [(point.day, point.submissions, point.exploited_submissions) for point in series] == [
        (days_ago(2), 4, 1),
        (days_ago(1), 0, 0),
        (TODAY, 2, 0),
    ]


def test_daily_series_of_an_empty_window_is_empty() -> None:
    assert _daily_series({days_ago(2): (4, 1)}, []) == []


def test_daily_series_over_its_own_days_is_the_sparse_form() -> None:
    # How the per-evaluation series is built: the same helper, given only the days it recorded.
    counts = {TODAY: (2, 0), days_ago(5): (4, 3)}

    series = _daily_series(counts, sorted(counts))

    assert [(point.day, point.submissions, point.exploited_submissions) for point in series] == [
        (days_ago(5), 4, 3),
        (TODAY, 2, 0),
    ]


def test_window_of_a_group_that_has_not_started_is_empty() -> None:
    assert _timeline_window(days_ahead(90), None, set(), today=TODAY) == []
    assert _timeline_window(days_ahead(90), days_ahead(120), set(), today=TODAY) == []


def _render_exploit_filter(eval_ids: list[UUID] | None) -> str:
    """The predicate as `_submissions_by_day` emits it.

    Inside an aggregate FILTER on a statement selecting from `message_flags` — the only position its
    plan claim is about. Compiled standalone it renders uncorrelated (`FROM reviews, message_flags`)
    and would pin a shape Postgres never runs.
    """
    statement = select(func.count().filter(_has_successful_exploit_review(eval_ids))).select_from(MessageFlag)
    return str(statement.compile(dialect=postgresql.dialect()))


def test_exploit_filter_scopes_the_subquery_when_given_evaluations() -> None:
    # A planner hint, not a behaviour: scoping the correlated EXISTS by evaluation is what makes
    # Postgres probe `ix_reviews_evaluation_id` instead of reading the whole `reviews` table inside
    # an aggregate FILTER. No functional test can see it, so the emitted SQL is the only thing that
    # can stop the predicate being deleted as redundant.
    scoped = _render_exploit_filter([uuid4(), uuid4()])

    assert "count(*) FILTER (WHERE EXISTS (" in scoped
    assert "reviews.message_flag_id = message_flags.id" in scoped
    assert "reviews.evaluation_id IN " in scoped
    # Correlated, so `reviews` is the subquery's only FROM entry.
    assert "FROM reviews, message_flags" not in scoped


def test_exploit_filter_omits_the_scope_by_default() -> None:
    unscoped = _render_exploit_filter(None)

    assert "reviews.successful_exploit IS true" in unscoped
    assert "evaluation_id" not in unscoped
