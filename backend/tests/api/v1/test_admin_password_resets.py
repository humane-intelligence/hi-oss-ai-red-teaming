"""Integration tests for the admin-triggered password-reset endpoints on `/v1/auth/users`."""

from itertools import pairwise
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

import app.core.email as email_module
from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import PasswordResetToken
from app.core.auth.models import PasswordResetTokenStatus
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.auth.services.password_resets import request_password_reset
from app.core.auth.services.users import create_user
from app.core.email.models import OutboundEmail
from app.core.notifications.models import Notification
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with
from tests.api.v1.conftest import make_role

_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def enqueue_stub(celery_enqueue_stub: MagicMock) -> MagicMock:
    """Module-wide: every path here dispatches mail. Aliases the shared spy under this module's name."""
    return celery_enqueue_stub


async def _target(
    db: AsyncSession,
    *,
    account_status: UserStatus = UserStatus.ACTIVE,
    password: str | None = _PASSWORD,
) -> User:
    role = await make_role(db)
    return await create_user(
        db,
        email=f"target-{uuid4().hex[:8]}@example.com",
        password=SecretStr(password) if password is not None else None,
        status=account_status,
        roles=[role],
    )


async def _pending_tokens(db: AsyncSession, user_id: UUID) -> list[PasswordResetToken]:
    result = await db.execute(
        PasswordResetToken.live_select()
        .where(col(PasswordResetToken.user_id) == user_id)
        .where(col(PasswordResetToken.status) == PasswordResetTokenStatus.PENDING)
    )
    return list(result.scalars().all())


@pytest.mark.integration
async def test_send_password_reset_issues_token_and_mails(
    auth_db_client: AsyncClient, db_session: AsyncSession, enqueue_stub: MagicMock
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    caller_id = caller.id
    target_id = target.id

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target_id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert len(await _pending_tokens(db_session, target_id)) == 1
    assert enqueue_stub.call_count == 1

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog).where(
                col(AuditLog.action) == AuditAction.USER_CREDENTIAL_RESET.value,
                col(AuditLog.object_id) == target_id,
            )
        )
    ).scalar_one()
    assert row.actor_id == caller_id


@pytest.mark.integration
async def test_send_password_reset_ignores_the_self_service_cooldown(
    auth_db_client: AsyncClient, db_session: AsyncSession, enqueue_stub: MagicMock
) -> None:
    """An admin acting deliberately is not the flood the cooldown exists for.

    It also has to work in the case that produces the throttle: a user clicking "forgot
    password" repeatedly and then asking an admin for help.
    """
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_id = target.id
    await request_password_reset(db_session, email=target.email)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target_id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert enqueue_stub.call_count == 2


@pytest.mark.integration
async def test_send_password_reset_marks_the_mail_as_admin_triggered(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The recipient never asked for this reset — the template must not claim they did."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_email = target.email

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    row = (
        await db_session.execute(select(OutboundEmail).where(col(OutboundEmail.recipient) == target_email))
    ).scalar_one()
    assert row.context["triggered_by_admin"] is True
    assert "reset_url" not in row.context  # the token never gets persisted


@pytest.mark.integration
async def test_send_password_reset_supersedes_a_pending_token(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_id = target.id
    headers = bearer(caller)

    first = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/password-reset", headers=headers)
    second = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/password-reset", headers=headers)

    assert (first.status_code, second.status_code) == (
        status.HTTP_204_NO_CONTENT,
        status.HTTP_204_NO_CONTENT,
    )
    assert len(await _pending_tokens(db_session, target_id)) == 1


@pytest.mark.integration
async def test_send_password_reset_allows_self_target(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Unlike a status change, resetting your own password locks nobody out."""
    role = await make_role(db_session, [Permission.USERS_UPDATE.value])
    caller = await create_user(
        db_session,
        email="self-reset@example.com",
        password=SecretStr(_PASSWORD),
        status=UserStatus.ACTIVE,
        roles=[role],
    )

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{caller.id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.integration
async def test_send_password_reset_surfaces_ineligibility_as_409(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Endpoint-specific mapping only — the eligibility branches are unit-tested on the service."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session, account_status=UserStatus.INACTIVE)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_send_password_reset_unknown_user_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{uuid4()}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_send_password_reset_forbidden_without_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session)
    target = await _target(db_session)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/password-reset",
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "users:update" in response.json()["detail"]


@pytest.mark.integration
async def test_bulk_send_password_resets_reports_per_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, enqueue_stub: MagicMock
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    eligible = await _target(db_session)
    inactive = await _target(db_session, account_status=UserStatus.INACTIVE)
    eligible_id = eligible.id

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={
            "rows": [
                {"row_key": "r1", "data": {"user_id": str(eligible_id)}},
                {"row_key": "r2", "data": {"user_id": str(inactive.id)}},
                {"row_key": "r3", "data": {"user_id": str(uuid4())}},
            ]
        },
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["total"], body["succeeded"], body["failed"]) == (3, 1, 2)
    by_key = {r["row_key"]: r for r in body["results"]}
    assert by_key["r1"]["data"]["user_id"] == str(eligible_id)
    assert by_key["r2"]["error"]["status"] == status.HTTP_409_CONFLICT
    assert by_key["r3"]["error"]["status"] == status.HTTP_404_NOT_FOUND

    assert len(await _pending_tokens(db_session, eligible_id)) == 1
    # Only the one eligible row produced mail — a failed row never reaches spec collection.
    assert enqueue_stub.call_count == 1


@pytest.mark.integration
async def test_bulk_send_password_resets_dry_run_sends_nothing(
    auth_db_client: AsyncClient, db_session: AsyncSession, enqueue_stub: MagicMock
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_id = target.id

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={"dry_run": True, "rows": [{"row_key": "r1", "data": {"user_id": str(target_id)}}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == 1
    enqueue_stub.assert_not_called()

    db_session.expire_all()
    assert await _pending_tokens(db_session, target_id) == []
    audited = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    col(AuditLog.action) == AuditAction.USER_CREDENTIAL_RESET.value,
                    col(AuditLog.object_id) == target_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert audited == []


@pytest.mark.integration
async def test_bulk_send_password_resets_forbidden_without_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={"rows": [{"row_key": "r1", "data": {"user_id": str(uuid4())}}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_bulk_send_password_resets_rejects_a_repeated_user(
    auth_db_client: AsyncClient, db_session: AsyncSession, enqueue_stub: MagicMock
) -> None:
    """Issuing a token revokes the previous one, so a second row would only mail a dead link."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={
            "rows": [
                {"row_key": "r1", "data": {"user_id": str(target.id)}},
                {"row_key": "r2", "data": {"user_id": str(target.id)}},
            ]
        },
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    enqueue_stub.assert_not_called()


@pytest.mark.integration
async def test_bulk_send_password_resets_writes_the_outbound_row(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The bulk path splits secret from persisted context the same way the single one does.

    Says nothing about the row being *committed* — a read through `db_session` sees that
    session's own uncommitted flush. `..._commits_after_dispatch` covers that.
    """
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_email = target.email

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={"rows": [{"row_key": "r1", "data": {"user_id": str(target.id)}}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = (
        await db_session.execute(select(OutboundEmail).where(col(OutboundEmail.recipient) == target_email))
    ).scalar_one()
    assert row.context["triggered_by_admin"] is True
    assert "reset_url" not in row.context


@pytest.mark.integration
async def test_bulk_send_password_resets_unsendable_mail_notifies_the_caller_once(
    auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three lost mails are one broken batch — one notice, not one per recipient.

    The tokens themselves still commit: dispatch runs after `apply_bulk`, and the
    resets are the outcome the operator asked for.
    """
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    targets = [await _target(db_session) for _ in range(3)]
    caller_id = caller.id
    monkeypatch.setattr(email_module, "send_email", MagicMock(side_effect=RuntimeError("render blew up")))

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={
            "rows": [{"row_key": f"r{i}", "data": {"user_id": str(t.id)}} for i, t in enumerate(targets)],
        },
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["failed"] == 0

    db_session.expire_all()
    notifications = (await db_session.execute(Notification.live_select())).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].user_id == caller_id
    assert "3 of 3" in (notifications[0].description or "")


@pytest.mark.integration
async def test_bulk_send_password_resets_commits_each_row_before_enqueueing_the_next(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two mails must not share one commit — the second would race the first's countdown.

    `send_email` enqueues with a 1-second countdown so the caller's commit lands first, so
    the gap between an enqueue and the commit of its `outbound_emails` row has to stay
    inside one iteration. Batching the commit until after the loop widens that gap to the
    whole dispatch and the worker drops the mail as `email.task.row_missing` — while the
    row still reports `ok`, because the enqueue itself succeeded. Only the interleaving
    shows it: a read through `db_session` sees its own uncommitted flush either way.
    """
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    targets = [await _target(db_session) for _ in range(2)]
    events: list[str] = []
    original = db_session.commit

    async def recording_commit() -> None:
        events.append("commit")
        await original()

    monkeypatch.setattr(db_session, "commit", recording_commit)
    enqueue_stub.side_effect = lambda *_args, **_kwargs: events.append("enqueue")

    response = await auth_db_client.post(
        "/api/v1/auth/users/password-reset",
        json={"rows": [{"row_key": f"r{i}", "data": {"user_id": str(t.id)}} for i, t in enumerate(targets)]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    assert events.count("enqueue") == len(targets)
    assert ("enqueue", "enqueue") not in pairwise(events), (
        f"a mail was enqueued before the previous row's commit: {events}"
    )
    # `apply_bulk`'s commit, one per row, and the trailing one that lands a lost-mail notice.
    assert events.count("commit") == len(targets) + 2, events
