"""API tests for `POST /api/v1/evaluation-groups/{group_id}/invitations` (+ `/bulk`).

The invite-by-email matrix: the two outcomes (immediate assign vs. token issue),
multi-role + reconcile-on-reinvite, the conflict/validation cases, and that
authorization mirrors member management (lesser-role member 403, outsider 404,
break-glass admin passes). The bulk envelope adds per-row isolation, dry-run
side-effect gating, duplicate-email rejection, and post-commit mail dispatch.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.email as email_module
from app.core.auth.dependencies import current_user
from app.core.auth.models import Invitation
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import held_roles
from app.core.auth.roles import Permission
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_user_by_email
from app.core.email.models import OutboundEmail
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.notifications.models import Notification
from app.main import app
from tests.conftest import session_user_from

pytestmark = pytest.mark.integration

_GROUP = ObjectType.EVALUATION_GROUP


@pytest.fixture(autouse=True)
def _celery_enqueue_stub(celery_enqueue_stub: MagicMock) -> None:
    """Every route here dispatches mail, so the shared spy is module-wide rather than per-test."""


@contextmanager
def as_user(user: User) -> Iterator[None]:
    app.dependency_overrides[current_user] = lambda: session_user_from(user)
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_user, None)


async def _user(
    db: AsyncSession, *, permissions: list[str] | None = None, status_: UserStatus = UserStatus.ACTIVE
) -> User:
    role = Role(name=f"r-{uuid4().hex[:8]}", description="test", permissions=permissions or [])
    db.add(role)
    await db.flush()
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role], status=status_)


async def _group(
    db: AsyncSession,
    *,
    owner: User,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.INVITATION_ONLY,
) -> EvaluationGroup:
    group = EvaluationGroup(
        title="Engagement",
        description="A red-teaming engagement.",
        created_by_id=owner.id,
        access_level=access_level,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return group


async def _owned_group(db: AsyncSession, system_roles: dict[str, Role], owner: User) -> EvaluationGroup:
    group = await _group(db, owner=owner)
    await grant_roles(db, _GROUP, group.id, owner.id, [system_roles["owner"]])
    return group


def _url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/invitations"


def _assert_problem(response: Response, expected_status: int) -> None:
    """Assert an RFC 7807 error envelope: problem+json media type + status/title body."""
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def test_removed_single_invite_endpoint_is_gone(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _url(group),
            json={"email": "ada@example.com", "role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /api/v1/evaluation-groups/{group_id}/invitations/bulk
# ---------------------------------------------------------------------------


def _bulk_url(group: EvaluationGroup) -> str:
    return f"{_url(group)}/bulk"


def _row(key: str, email: str, *roles: Role) -> dict:
    return {"row_key": key, "data": {"email": email, "role_ids": [str(role.id) for role in roles]}}


async def test_bulk_invite_mixed_outcomes_commit_both_rows(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    active = await create_user(
        db_session, email="ada@example.com", status=UserStatus.ACTIVE, roles=[system_roles["red_teamer"]]
    )

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("new", "bea@example.com", system_roles["red_teamer"]),
                    _row("active", active.email, system_roles["viewer"]),
                ],
            },
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is False
    assert (body["total"], body["succeeded"], body["failed"]) == (2, 2, 0)

    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["new"]["data"]["outcome"] == "invited"
    assert by_key["active"]["data"]["outcome"] == "assigned"

    new_user = await get_user_by_email(db_session, "bea@example.com")
    assert new_user is not None
    assert [role.name for role in await held_roles(db_session, _GROUP, group.id, new_user.id)] == ["red_teamer"]
    assert [role.name for role in await held_roles(db_session, _GROUP, group.id, active.id)] == ["viewer"]
    # One invitation mail (invited branch) + one member-added mail (assigned branch).
    assert celery_enqueue_stub.call_count == 2


async def test_bulk_invite_per_row_failure_is_isolated(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    deactivated = await create_user(
        db_session, email="off@example.com", status=UserStatus.INACTIVE, roles=[system_roles["red_teamer"]]
    )

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("ok", "bea@example.com", system_roles["viewer"]),
                    _row("conflict", deactivated.email, system_roles["viewer"]),
                ],
            },
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 1)

    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["ok"]["status"] == "ok"
    assert by_key["conflict"]["status"] == "failed"
    assert by_key["conflict"]["error"]["status"] == status.HTTP_409_CONFLICT

    new_user = await get_user_by_email(db_session, "bea@example.com")
    assert new_user is not None
    assert await held_roles(db_session, _GROUP, group.id, new_user.id) != []
    assert await held_roles(db_session, _GROUP, group.id, deactivated.id) == []
    # Only the committed row's mail was dispatched.
    assert celery_enqueue_stub.call_count == 1


async def test_bulk_invite_dry_run_skips_email_and_persistence(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    # Dry-run rolls the whole session transaction back, so post-request
    # assertions must be emptiness checks — fixture rows are gone too.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [_row("preview", "bea@example.com", system_roles["red_teamer"])],
                "dry_run": True,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is True
    assert (body["succeeded"], body["failed"]) == (1, 0)
    assert body["results"][0]["data"]["outcome"] == "invited"

    assert await get_user_by_email(db_session, "bea@example.com") is None
    invitations = (await db_session.execute(Invitation.live_select())).scalars().all()
    assert invitations == []
    emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    assert emails == []
    celery_enqueue_stub.assert_not_called()


async def test_bulk_invite_duplicate_email_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Two rows for one address would mail the invitee a dead accept link (the
    # later row revokes the earlier token) — rejected like a duplicate row_key.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("first", "bea@example.com", system_roles["viewer"]),
                    _row("second", "Bea@Example.com", system_roles["annotator"]),
                ],
            },
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert await get_user_by_email(db_session, "bea@example.com") is None


async def test_bulk_invite_duplicate_row_key_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("dup", "a@example.com", system_roles["viewer"]),
                    _row("dup", "b@example.com", system_roles["viewer"]),
                ],
            },
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_bulk_invite_empty_rows_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(_bulk_url(group), json={"rows": []})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_bulk_invite_member_with_lesser_role_gets_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await grant_roles(db_session, _GROUP, group.id, member.id, [system_roles["red_teamer"]])

    with as_user(member):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("r1", "bea@example.com", system_roles["viewer"])]},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_bulk_invite_outsider_gets_404_on_private_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    outsider = await _user(db_session)

    with as_user(outsider):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("r1", "bea@example.com", system_roles["viewer"])]},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_bulk_invited_accept_url_never_persisted_rides_task_signature(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    # The invited branch's accept URL carries the raw onboarding token; it must
    # stay out of the OutboundEmail audit row and ride the task signature only.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("r1", "ada@example.com", system_roles["red_teamer"])]},
        )

    assert response.status_code == status.HTTP_200_OK

    emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    assert len(emails) == 1
    assert "accept_url" not in emails[0].context

    transient = celery_enqueue_stub.call_args.kwargs["args"][1]
    assert "accept_url" in transient


async def test_bulk_reinvite_demoting_last_owner_fails_the_row_with_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The reconcile path goes through `set_member_roles`, so re-inviting the sole
    # owner with a role set that drops `owner` would leave the group ownerless —
    # the last-owner guard rejects it, now as a per-row error.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("demote", owner.email, system_roles["red_teamer"])]},
        )

    assert response.status_code == status.HTTP_200_OK
    [row] = response.json()["results"]
    assert row["status"] == "failed"
    assert row["error"]["status"] == status.HTTP_409_CONFLICT


async def test_bulk_invite_unsendable_mail_notifies_the_caller_once(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three lost mails are one broken batch, so the caller gets one notice, not three.

    The rows themselves still commit — dispatch runs after `apply_bulk`, and the
    invitations are the outcome the operator asked for.
    """
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    monkeypatch.setattr(email_module, "send_email", MagicMock(side_effect=RuntimeError("render blew up")))

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("a", "a@example.com", system_roles["red_teamer"]),
                    _row("b", "b@example.com", system_roles["red_teamer"]),
                    _row("c", "c@example.com", system_roles["red_teamer"]),
                ],
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["failed"] == 0

    notifications = (await db_session.execute(Notification.live_select())).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].user_id == owner.id
    assert "3 of 3" in (notifications[0].description or "")


async def test_bulk_invite_non_assignable_role_fails_the_row_with_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The route advertises 400 for a non-assignable role; only the bulk path can reach it now."""
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("elevated", "ada@example.com", system_roles["admin"])]},
        )

    assert response.status_code == status.HTTP_200_OK
    [row] = response.json()["results"]
    assert row["status"] == "failed"
    assert row["error"]["status"] == status.HTTP_400_BAD_REQUEST


async def test_bulk_reinvite_active_member_reconciles_roles(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Re-inviting an existing member reconciles the role set wholesale, like the members PATCH."""
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    invitee = await create_user(
        db_session, email="ada@example.com", status=UserStatus.ACTIVE, roles=[system_roles["red_teamer"]]
    )
    await grant_roles(db_session, _GROUP, group.id, invitee.id, [system_roles["viewer"]])

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("reconcile", invitee.email, system_roles["annotator"])]},
        )

    assert response.status_code == status.HTTP_200_OK
    [row] = response.json()["results"]
    assert row["status"] == "ok"
    assert row["data"]["outcome"] == "assigned"
    assert [role["name"] for role in row["data"]["roles"]] == ["annotator"]
    assert [role.name for role in await held_roles(db_session, _GROUP, group.id, invitee.id)] == ["annotator"]


async def test_bulk_invite_break_glass_admin_can_invite(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    admin = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE.value])

    with as_user(admin):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("r1", "ada@example.com", system_roles["red_teamer"])]},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == 1


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"email": "ada@example.com"}, id="missing-roles"),
        pytest.param({"email": "ada@example.com", "role_ids": []}, id="empty-roles"),
    ],
)
async def test_bulk_invite_rejects_malformed_row(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    data: dict[str, object],
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [{"row_key": "bad", "data": data}]},
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_bulk_invite_over_the_row_cap_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    rows = [_row(f"r{i}", f"u{i}@example.com", system_roles["red_teamer"]) for i in range(MAX_INVITE_ROWS + 1)]

    with as_user(owner):
        response = await async_client_with_db.post(_bulk_url(group), json={"rows": rows, "dry_run": True})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
