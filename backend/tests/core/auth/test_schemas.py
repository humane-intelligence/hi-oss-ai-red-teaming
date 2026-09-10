"""Pure-logic tests for `app.core.auth.schemas`."""

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.schemas import InvitationCreate
from app.core.auth.schemas import UserInvitationInfo


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supplied", "canonical"),
    [
        ("Ada@Example.com", "ada@example.com"),
        ("ADA@EXAMPLE.COM", "ada@example.com"),
        ("  ada@example.com  ", "ada@example.com"),
        ("ada@example.com", "ada@example.com"),
    ],
)
def test_normalized_email_lowercases_and_strips(supplied: str, canonical: str) -> None:
    payload = InvitationCreate(
        email=supplied,
        role_ids=[uuid.UUID("a1b2c3d4-1111-2222-3333-444455556666")],
    )

    assert payload.email == canonical


def _invitation(status: InvitationStatus, expires_at: datetime) -> Invitation:
    return Invitation(
        user_id=uuid.uuid4(),
        token_hash="0" * 64,
        expires_at=expires_at,
        status=status,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored", "age", "expected"),
    [
        (InvitationStatus.PENDING, timedelta(hours=1), InvitationStatus.PENDING),
        (InvitationStatus.PENDING, timedelta(hours=-1), InvitationStatus.EXPIRED),
        (InvitationStatus.ACCEPTED, timedelta(hours=-1), InvitationStatus.ACCEPTED),
        (InvitationStatus.REVOKED, timedelta(hours=1), InvitationStatus.REVOKED),
    ],
)
def test_user_invitation_info_projects_overdue_pending_as_expired(
    stored: InvitationStatus, age: timedelta, expected: InvitationStatus
) -> None:
    expires_at = datetime.now(UTC) + age

    info = UserInvitationInfo.from_invitation(_invitation(stored, expires_at))

    assert info.status is expected
    assert info.expires_at == expires_at
