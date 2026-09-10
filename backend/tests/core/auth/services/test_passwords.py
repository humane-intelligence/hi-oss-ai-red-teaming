"""Unit tests for `app.core.auth.services.passwords`."""

import pytest

from app.core.auth.services.passwords import hash_password
from app.core.auth.services.passwords import verify_password


@pytest.mark.unit
def test_hash_password_is_not_plaintext() -> None:
    hashed = hash_password("correct-horse-battery-staple")

    assert hashed != "correct-horse-battery-staple"
    assert hashed.startswith("$argon2")


@pytest.mark.unit
def test_hash_password_produces_unique_hashes() -> None:
    a = hash_password("same-password")
    b = hash_password("same-password")

    assert a != b


@pytest.mark.unit
def test_verify_password_accepts_correct_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")

    assert verify_password("correct-horse-battery-staple", hashed) is True


@pytest.mark.unit
def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")

    assert verify_password("wrong-password", hashed) is False
