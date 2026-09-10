"""Tests for app.core.email.send_email — async public entry point."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from jinja2 import TemplateNotFound
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.email as email_module
from app.core.auth.models import EmailVerification
from app.core.auth.models import Role
from app.core.auth.services.users import create_user
from app.core.email import send_email
from app.core.email import send_email_best_effort
from app.core.email import tasks as tasks_module
from app.core.email.models import OutboundEmail
from app.core.email.models import OutboundEmailStatus
from app.core.notifications.models import Notification


@pytest.mark.integration
async def test_send_email_inserts_queued_row_and_enqueues_task(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    enqueue = MagicMock()
    monkeypatch.setattr(tasks_module.send_email_task, "apply_async", enqueue)

    email_id = await send_email(db_session, "account_activated", "rcpt@example.com", {"user_name": "Ada"})

    row = await db_session.get(OutboundEmail, email_id)
    assert row is not None
    assert row.status == OutboundEmailStatus.QUEUED
    assert row.recipient == "rcpt@example.com"
    assert row.template_name == "account_activated"
    assert row.context == {"user_name": "Ada", "password_cleared": False}
    assert row.subject == "Welcome to AI Red Teaming, Ada"  # set at call-time from the rendered template
    enqueue.assert_called_once()
    args = enqueue.call_args.kwargs.get("args") or enqueue.call_args.args[0]
    assert args == [str(email_id), {}]  # no secrets → empty transient payload


@pytest.mark.integration
async def test_send_email_keeps_secret_context_out_of_persisted_row(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """`secret_context` is validated but never persisted; it rides the task signature."""
    enqueue = MagicMock()
    monkeypatch.setattr(tasks_module.send_email_task, "apply_async", enqueue)

    accept_url = "https://app.example.com/accept-invitation?token=secret-abc"
    email_id = await send_email(
        db_session,
        "platform_invitation",
        "rcpt@example.com",
        {
            "inviter_name": "Olive",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "role_names": ["Red Teamer"],
        },
        secret_context={"accept_url": accept_url},
    )

    row = await db_session.get(OutboundEmail, email_id)
    assert row is not None
    assert "accept_url" not in row.context  # the bearer secret is not in the audit row
    assert set(row.context) == {"inviter_name", "expires_at", "role_names"}

    transient = enqueue.call_args.kwargs["args"][1]
    assert transient == {"accept_url": accept_url}  # secret travels via the task signature


@pytest.mark.integration
async def test_send_email_rejects_invalid_context(db_session: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await send_email(db_session, "account_activated", "rcpt@example.com", {})


@pytest.mark.integration
async def test_send_email_rejects_unknown_template(db_session: AsyncSession) -> None:
    with pytest.raises(KeyError):
        await send_email(db_session, "no-such-template", "rcpt@example.com", {})


@pytest.mark.integration
async def test_send_email_rejects_invalid_recipient(db_session: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="value is not a valid email"):
        await send_email(db_session, "account_activated", "not-an-email", {"user_name": "Ada"})


@pytest.mark.integration
async def test_send_email_propagates_render_errors_and_skips_insert(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A render failure must surface synchronously and not leave an audit row behind."""
    enqueue = MagicMock()
    monkeypatch.setattr(tasks_module.send_email_task, "apply_async", enqueue)
    monkeypatch.setattr(
        email_module, "render_template", MagicMock(side_effect=TemplateNotFound("account_activated/subject.txt"))
    )

    with pytest.raises(TemplateNotFound):
        await send_email(db_session, "account_activated", "rcpt@example.com", {"user_name": "Ada"})

    rows = (await db_session.execute(select(OutboundEmail))).scalars().all()
    assert list(rows) == []
    enqueue.assert_not_called()


@pytest.mark.integration
async def test_send_email_best_effort_delegates_on_success(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """The wrapper must not alter the success path — same row, same enqueue."""
    enqueue = MagicMock()
    monkeypatch.setattr(tasks_module.send_email_task, "apply_async", enqueue)

    await send_email_best_effort(db_session, "account_activated", "rcpt@example.com", {"user_name": "Ada"})

    row = (await db_session.execute(select(OutboundEmail))).scalars().one()
    assert row.template_name == "account_activated"
    assert row.recipient == "rcpt@example.com"
    enqueue.assert_called_once()


@pytest.mark.integration
async def test_send_email_best_effort_swallows_flush_failure_and_leaves_session_usable(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Policy regression: a session-poisoning failure inside `send_email` must be confined.

    Reproduces the documented failure mode by patching `send_email` to add a row
    with a bogus FK and then flush — exactly what a real `OutboundEmail` insert
    failure looks like to the surrounding transaction. The wrapper's SAVEPOINT
    must roll the bad row back without poisoning the outer session; we assert
    that by performing a normal follow-up insert + flush after the swallow.
    """

    async def _poison_flush(session: AsyncSession, *_args: object, **_kwargs: object) -> None:
        session.add(
            EmailVerification(
                user_id=uuid4(),  # no such user → FK violation on flush
                token_hash="poison-token-hash",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        await session.flush()

    monkeypatch.setattr(email_module, "send_email", _poison_flush)

    # Must not raise.
    await send_email_best_effort(db_session, "account_activated", "rcpt@example.com", {"user_name": "Ada"})

    # If the SAVEPOINT had leaked, this flush would raise `InvalidRequestError`
    # ("This Session's transaction has been rolled back…"). A clean flush is the
    # contract: post-state-transition work after the wrapper must still commit.
    await db_session.flush()
    rows = (await db_session.execute(select(OutboundEmail))).scalars().all()
    assert list(rows) == []  # SAVEPOINT rolled back the poison; no audit row leaked either


@pytest.mark.integration
async def test_send_email_best_effort_notifies_the_requester_when_nothing_was_queued(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """The swallowed branch rolls the `OutboundEmail` back, so the worker never runs.

    That makes this the only chance to tell the requester — nothing downstream will.
    """
    role = Role(name=f"role-{uuid4().hex[:8]}", description="test", permissions=[])
    db_session.add(role)
    await db_session.flush()
    requester = await create_user(db_session, email="requester@example.com", roles=[role])
    monkeypatch.setattr(email_module, "send_email", MagicMock(side_effect=RuntimeError("render blew up")))

    await send_email_best_effort(
        db_session,
        "account_activated",
        "rcpt@example.com",
        {"user_name": "Ada"},
        requested_by_user_id=requester.id,
    )

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].user_id == requester.id
    assert "rcpt@example.com" in (notifications[0].description or "")
