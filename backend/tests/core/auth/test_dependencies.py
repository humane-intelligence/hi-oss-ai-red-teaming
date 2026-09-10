"""Unit tests for `require_permission` enforcement."""

from uuid import uuid4

import pytest

from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.exceptions import ForbiddenError


def _session_user(permissions: frozenset[str]) -> SessionUser:
    return SessionUser(
        id=uuid4(),
        email="ada@example.com",
        email_verified=True,
        first_name="Ada",
        last_name="Lovelace",
        provider="local",
        permissions=permissions,
    )


@pytest.mark.unit
def test_require_permission_passes_when_held() -> None:
    user = _session_user(frozenset({"users:read"}))

    result = require_permission(Permission.USERS_READ)(user)

    assert result is user


@pytest.mark.unit
def test_require_permission_raises_403_when_missing() -> None:
    user = _session_user(frozenset({"users:read"}))

    with pytest.raises(ForbiddenError, match=r"Caller lacks the 'users:delete' permission\."):
        require_permission(Permission.USERS_DELETE)(user)
