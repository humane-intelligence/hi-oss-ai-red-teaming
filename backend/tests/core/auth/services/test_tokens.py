"""Pure-logic tests for `app.core.auth.services.tokens`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.auth.models import PasswordResetTokenStatus
from app.core.auth.services.tokens import project_expired


@pytest.mark.unit
def test_project_expired_surfaces_overdue_pending_as_expired() -> None:
    overdue = datetime.now(UTC) - timedelta(seconds=1)
    result = project_expired(
        PasswordResetTokenStatus.PENDING,
        overdue,
        pending=PasswordResetTokenStatus.PENDING,
        expired=PasswordResetTokenStatus.EXPIRED,
    )
    assert result is PasswordResetTokenStatus.EXPIRED


@pytest.mark.unit
def test_project_expired_keeps_fresh_pending() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    result = project_expired(
        PasswordResetTokenStatus.PENDING,
        future,
        pending=PasswordResetTokenStatus.PENDING,
        expired=PasswordResetTokenStatus.EXPIRED,
    )
    assert result is PasswordResetTokenStatus.PENDING


@pytest.mark.unit
def test_project_expired_leaves_non_pending_untouched() -> None:
    overdue = datetime.now(UTC) - timedelta(hours=1)
    result = project_expired(
        PasswordResetTokenStatus.USED,
        overdue,
        pending=PasswordResetTokenStatus.PENDING,
        expired=PasswordResetTokenStatus.EXPIRED,
    )
    assert result is PasswordResetTokenStatus.USED
