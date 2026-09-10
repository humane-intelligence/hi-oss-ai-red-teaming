"""Pure-logic tests for `app.core.helpers`."""

import pytest

from app.core.helpers import escape_like


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("", ""),
        ("plain", "plain"),
        ("50%", "50\\%"),
        ("a_b", "a\\_b"),
        ("c:\\path", "c:\\\\path"),
        ("100% _safe_ \\o/", "100\\% \\_safe\\_ \\\\o/"),
    ],
)
def test_escape_like_backslashes_wildcards(raw: str, escaped: str) -> None:
    assert escape_like(raw) == escaped


@pytest.mark.unit
def test_escape_like_orders_backslash_before_wildcards() -> None:
    # The backslash must be escaped first; otherwise the `\` we insert in
    # front of `%`/`_` would itself be doubled and break the escape.
    assert escape_like("%") == "\\%"
    assert escape_like("\\%") == "\\\\\\%"
