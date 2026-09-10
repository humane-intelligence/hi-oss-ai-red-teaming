"""Integration tests for `/v1/auth/invitations` router — create, verify, accept."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.services.invitations import invite_to_platform
from app.core.auth.services.users import create_user as create_user_service
from app.core.email.models import OutboundEmail
from app.core.platform_settings.service import update_platform_settings
from tests.api.v1.conftest import build_settings as _settings
from tests.api.v1.conftest import make_token as _token
from tests.conftest import session_user_from


@pytest.fixture(autouse=True)
def _celery_enqueue_stub(celery_enqueue_stub: MagicMock) -> None:
    """Every invite path here dispatches mail, so the shared spy is module-wide."""


@pytest_asyncio.fixture
async def annotator_role(db_session: AsyncSession) -> Role:
    role = Role(name="annotator", permissions=["evaluations:annotate"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def red_teamer_role(db_session: AsyncSession) -> Role:
    role = Role(name="red_teamer", display_name="Red Teamer", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(name="admin", permissions=sorted(ROLE_PERMISSIONS[SystemRole.ADMIN]))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def owner_role(db_session: AsyncSession) -> Role:
    role = Role(name="owner", permissions=sorted(ROLE_PERMISSIONS[SystemRole.OWNER]))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession, admin_role: Role) -> User:
    return await create_user_service(
        db_session,
        email="admin@example.com",
        first_name="Bob",
        last_name="Smith",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )


@pytest_asyncio.fixture
async def owner_user(db_session: AsyncSession, owner_role: Role) -> User:
    return await create_user_service(
        db_session,
        email="owner@example.com",
        first_name="Olive",
        last_name="Owner",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[owner_role],
    )


# ---------------------------------------------------------------------------
# POST /api/v1/auth/invitations/bulk — the only invite path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_single_row_invite_projects_the_invitation_without_a_token(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """Inviting one person is a one-row request — the row carries the same projection the route used to return."""
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "one", "data": {"email": "Ada@Example.COM", "role_ids": [str(annotator_role.id)]}},
            ],
        },
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    [row] = response.json()["results"]
    assert row["status"] == "ok"
    assert row["data"]["email"] == "ada@example.com"
    assert row["data"]["status"] == InvitationStatus.PENDING.value
    assert row["data"]["invited_by_user_id"] == str(admin_user.id)
    assert "token" not in row["data"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"email": "ada@example.com", "role_ids": []}, id="empty-role-list"),
        pytest.param({"email": "not-an-email", "role_ids": ["a1b2c3d4-1111-2222-3333-444455556666"]}, id="bad-email"),
    ],
)
async def test_bulk_invite_rejects_malformed_row(
    auth_db_client: AsyncClient,
    admin_user: User,
    data: dict[str, object],
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={"rows": [{"row_key": "bad", "data": data}]},
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_bulk_invite_ungrantable_role_lands_per_row_403(
    auth_db_client: AsyncClient,
    owner_user: User,
    admin_role: Role,
    red_teamer_role: Role,
) -> None:
    """Bulk: a row assigning a role the caller can't grant fails as 403 inside `results`."""
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "ok", "data": {"email": "rt@example.com", "role_ids": [str(red_teamer_role.id)]}},
                {"row_key": "denied", "data": {"email": "adm@example.com", "role_ids": [str(admin_role.id)]}},
            ],
        },
        headers={"Authorization": f"Bearer {_token(owner_user)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    by_key = {row["row_key"]: row for row in response.json()["results"]}
    assert by_key["ok"]["status"] == "ok"
    assert by_key["denied"]["status"] == "failed"
    assert by_key["denied"]["error"]["status"] == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# GET /api/v1/auth/invitations/accept
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_accept_returns_preview(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.get(
        "/api/v1/auth/invitations/accept",
        params={"token": issued.raw_token},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["email"] == "ada@example.com"
    assert body["role_names"] == ["annotator"]
    assert body["inviter_name"] == "Bob Smith"
    assert "expires_at" in body


@pytest.mark.integration
async def test_get_accept_preview_uses_role_display_name(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    red_teamer_role: Role,
) -> None:
    """`role_names` renders the human-readable `display_name`, not the slug."""
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[red_teamer_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.get(
        "/api/v1/auth/invitations/accept",
        params={"token": issued.raw_token},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["role_names"] == ["Red Teamer"]


@pytest.mark.integration
async def test_get_accept_overdue_pending_returns_410_without_db_write(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """410 for overdue PENDING; stored status stays `pending` (lazy expiry is read-only)."""
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )
    issued.invitation.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add(issued.invitation)
    await db_session.flush()
    invitation_id = issued.invitation.id

    response = await auth_db_client.get(
        "/api/v1/auth/invitations/accept",
        params={"token": issued.raw_token},
    )

    assert response.status_code == status.HTTP_410_GONE
    refreshed = await db_session.get(Invitation, invitation_id)
    assert refreshed is not None
    assert refreshed.status is InvitationStatus.PENDING


@pytest.mark.integration
async def test_post_accept_activates_user_and_returns_204(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={
            "token": issued.raw_token,
            "password": "supersecret-12345",
            "first_name": "Ada",
            "last_name": "Lovelace",
        },
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""

    user_row = (await db_session.execute(User.live_select().where(col(User.email) == "ada@example.com"))).scalar_one()
    assert user_row.status is UserStatus.ACTIVE
    assert user_row.email_verified_at is not None
    assert user_row.first_name == "Ada"
    inv_row = await db_session.get(Invitation, issued.invitation.id)
    assert inv_row is not None
    assert inv_row.status is InvitationStatus.ACCEPTED


@pytest.mark.integration
async def test_post_accept_short_password_returns_422(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": issued.raw_token, "password": "short"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_post_accept_honours_the_configured_character_classes(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    await update_platform_settings(db_session, password_require_uppercase=True)
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": issued.raw_token, "password": "uncommon-passphrase-42"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_missing_uppercase"


@pytest.mark.integration
async def test_post_accept_common_password_returns_400(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    issued = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(admin_user),
        send_side_effects=False,
    )

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": issued.raw_token, "password": "password1234"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_too_common"


async def _count_outbound_emails(session: AsyncSession) -> int:
    result = await session.execute(select(OutboundEmail))
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# POST /api/v1/auth/invitations/bulk
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_bulk_invite_mixed_success_and_per_row_conflict(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """One new email succeeds; a row targeting an already-active user fails as 409 inside `results`."""
    active = await create_user_service(
        db_session,
        email="ada@example.com",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[annotator_role],
    )

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "new", "data": {"email": "bea@example.com", "role_ids": [str(annotator_role.id)]}},
                {"row_key": "active", "data": {"email": active.email, "role_ids": [str(annotator_role.id)]}},
            ],
        },
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["dry_run"] is False

    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["new"]["status"] == "ok"
    assert by_key["new"]["data"]["email"] == "bea@example.com"
    assert by_key["active"]["status"] == "failed"
    assert by_key["active"]["error"]["status"] == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_bulk_invite_dry_run_skips_email_and_persistence(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """`dry_run=true` rolls back the outer transaction and gates `send_email` — no row, no audit entry."""
    before_emails = await _count_outbound_emails(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "preview", "data": {"email": "ada@example.com", "role_ids": [str(annotator_role.id)]}},
            ],
            "dry_run": True,
        },
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is True
    assert body["succeeded"] == 1
    assert body["failed"] == 0

    after_emails = await _count_outbound_emails(db_session)
    assert after_emails == before_emails

    invitations = (await db_session.execute(Invitation.live_select())).scalars().all()
    assert invitations == []
    users = (await db_session.execute(User.live_select().where(col(User.email) == "ada@example.com"))).scalars().all()
    assert users == []


@pytest.mark.integration
async def test_bulk_invite_over_max_rows_returns_422(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BulkRequest` reads `bulk_max_rows` lazily — re-monkeypatch and a 3-row payload trips the limit."""
    settings = _settings()
    settings.bulk_max_rows = 2
    # `bulk.py` imported `get_settings` as a local name, so patching the
    # config module alone doesn't affect the validator's call site.
    monkeypatch.setattr("app.core.bulk.get_settings", lambda: settings)

    rows = [
        {"row_key": f"r{i}", "data": {"email": f"u{i}@example.com", "role_ids": [str(annotator_role.id)]}}
        for i in range(3)
    ]
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={"rows": rows},
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_bulk_invite_duplicate_row_key_returns_422(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "dup", "data": {"email": "a@example.com", "role_ids": [str(annotator_role.id)]}},
                {"row_key": "dup", "data": {"email": "b@example.com", "role_ids": [str(annotator_role.id)]}},
            ],
        },
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_bulk_invite_at_the_row_cap_is_accepted(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """`dry_run` keeps the boundary case cheap: the cap is a validation rule, not a persistence one."""
    rows = [
        {"row_key": f"r{i}", "data": {"email": f"u{i}@example.com", "role_ids": [str(annotator_role.id)]}}
        for i in range(MAX_INVITE_ROWS)
    ]

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={"rows": rows, "dry_run": True},
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == MAX_INVITE_ROWS


@pytest.mark.integration
async def test_bulk_invite_over_the_row_cap_returns_422(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    rows = [
        {"row_key": f"r{i}", "data": {"email": f"u{i}@example.com", "role_ids": [str(annotator_role.id)]}}
        for i in range(MAX_INVITE_ROWS + 1)
    ]

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={"rows": rows, "dry_run": True},
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_bulk_invite_duplicate_email_returns_422(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    """Two rows for one address would mail a dead link — the later row revokes the earlier row's token."""
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/bulk",
        json={
            "rows": [
                {"row_key": "first", "data": {"email": "ada@example.com", "role_ids": [str(annotator_role.id)]}},
                {"row_key": "second", "data": {"email": "Ada@example.com", "role_ids": [str(annotator_role.id)]}},
            ],
        },
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_removed_single_invite_endpoint_is_gone(
    auth_db_client: AsyncClient,
    admin_user: User,
    annotator_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/invitations",
        json={"email": "ada@example.com", "role_ids": [str(annotator_role.id)]},
        headers={"Authorization": f"Bearer {_token(admin_user)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
