"""Unit tests for `app.core.auth.services.password_resets` branch logic.

The token lifecycle itself is covered API-first in `tests/api/v1/test_password_resets.py`;
this file owns the pure eligibility rule the admin-triggered endpoints gate on.
"""

from datetime import UTC
from datetime import datetime

import pytest

from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.password_resets import assert_password_reset_allowed
from app.core.exceptions import ConflictError


@pytest.mark.unit
def test_assert_password_reset_allowed_accepts_an_active_account_with_a_password() -> None:
    user = User(email="ada@example.com", status=UserStatus.ACTIVE, password="hashed")

    assert_password_reset_allowed(user)  # no raise


@pytest.mark.unit
@pytest.mark.parametrize("account_status", [UserStatus.INVITED, UserStatus.PENDING, UserStatus.INACTIVE])
def test_assert_password_reset_allowed_rejects_a_non_active_account(account_status: UserStatus) -> None:
    user = User(email="ada@example.com", status=account_status, password="hashed")

    with pytest.raises(ConflictError, match="only an active account"):
        assert_password_reset_allowed(user)


@pytest.mark.unit
def test_assert_password_reset_allowed_rejects_a_passwordless_account() -> None:
    """An OIDC-only account has nothing to reset — the link would be a dead end."""
    user = User(email="ada@example.com", status=UserStatus.ACTIVE, password=None)

    with pytest.raises(ConflictError, match="identity provider"):
        assert_password_reset_allowed(user)


@pytest.mark.unit
def test_assert_password_reset_allowed_rejects_a_recovering_account_too() -> None:
    """Unlike `request_password_reset`, the admin-triggered path doesn't carve out an
    exception for `password_cleared_at` — an admin sending a reset link to an account
    that never asked for one stays refused regardless of shape.
    """
    user = User(email="ada@example.com", status=UserStatus.ACTIVE, password=None, password_cleared_at=datetime.now(UTC))

    with pytest.raises(ConflictError, match="identity provider"):
        assert_password_reset_allowed(user)
