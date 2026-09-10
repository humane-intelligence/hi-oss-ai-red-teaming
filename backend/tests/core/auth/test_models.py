"""Pure-logic tests for `app.core.auth.models`."""

import pytest

from app.core.auth.models import User


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first_name", "last_name", "expected"),
    [
        ("Ada", "Lovelace", "Ada Lovelace"),
        ("Ada", None, "Ada"),
        (None, "Lovelace", "Lovelace"),
        (None, None, None),
        ("", "", None),
    ],
)
def test_user_full_name(first_name: str | None, last_name: str | None, expected: str | None) -> None:
    user = User(email="ada@example.com", first_name=first_name, last_name=last_name)
    assert user.full_name == expected
