"""Tests for app.core.email.tasks.send_email_task — task lifecycle against a real OutboundEmail row."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from celery.exceptions import Retry
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import col

from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.config import get_settings
from app.core.email import tasks as tasks_module
from app.core.email.backends import EmailMessage
from app.core.email.backends import TransientEmailError
from app.core.email.models import OutboundEmail
from app.core.email.models import OutboundEmailStatus
from app.core.email.tasks import send_email_task
from app.core.notifications.models import Notification
from app.workers.celery_app import app as celery_app

_REQUESTER_EMAIL = "requester@example.com"


@pytest.fixture
def patched_session(
    monkeypatch: pytest.MonkeyPatch,
    _test_db: str,
    db_engine: AsyncEngine,
) -> Iterator[Session]:
    """Patch the worker's sync session_scope to a session bound to the test DB.

    The async fixture stack (`db_session` / SAVEPOINT rollback) is unusable
    here because the production task uses a sync `session_scope`. We build
    a sync engine against the **test** database (`_test_db`) so the row is
    persisted and visible to the test, while staying isolated from any
    local / production DB. Per-test cleanup deletes every OutboundEmail this
    test inserted.
    """
    settings = get_settings()
    sync_url = make_url(settings.database_url_sync).set(database=_test_db).render_as_string(hide_password=False)
    engine = create_engine(sync_url, poolclass=NullPool)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(tasks_module, "session_scope", fake_scope)

    # Hand out a separate session for the test body to read with.
    test_session = session_factory()
    try:
        yield test_session
    finally:
        # Clean up rows this test inserted, then close. Notifications before emails:
        # a delivery-failure notification is written alongside the mail row, and users
        # last — both reference it.
        test_session.query(Notification).delete()
        test_session.query(OutboundEmail).delete()
        # Only the requester rows this module inserts — the DB is shared per xdist worker.
        test_session.query(User).filter(col(User.email) == _REQUESTER_EMAIL).delete()
        test_session.commit()
        test_session.close()
        engine.dispose()


def _insert_queued(session: Session, context: dict[str, Any] | None = None) -> OutboundEmail:
    email = OutboundEmail(
        template_name="account_activated",
        recipient="rcpt@example.com",
        context=context or {"user_name": "Ada"},
        backend="console",
        subject="Your account is now active, Ada",  # mirrors the row state `send_email()` produces
    )
    session.add(email)
    session.commit()
    session.refresh(email)
    return email


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_marks_row_as_sent(monkeypatch: pytest.MonkeyPatch, patched_session: Session) -> None:
    backend = MagicMock()
    backend.send.return_value = "provider-msg-1"
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = _insert_queued(patched_session)
    send_email_task.delay(str(email.id)).get(timeout=5)

    patched_session.refresh(email)
    assert email.status == OutboundEmailStatus.SENT
    assert email.sent_at is not None
    assert email.attempts == 1
    assert email.subject == "Your account is now active, Ada"
    assert email.error_type is None
    assert email.provider_message_id == "provider-msg-1"
    sent_message: EmailMessage = backend.send.call_args.args[0]
    assert sent_message.to == "rcpt@example.com"


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_records_permanent_failure(monkeypatch: pytest.MonkeyPatch, patched_session: Session) -> None:
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = _insert_queued(patched_session)
    with pytest.raises(RuntimeError):
        send_email_task.delay(str(email.id)).get(timeout=5)

    patched_session.refresh(email)
    assert email.status == OutboundEmailStatus.FAILED
    assert email.error_type == "RuntimeError"
    assert email.error_message == "smtp 550 rejected"
    assert email.attempts == 1


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_transient_at_retry_budget_marks_failed(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """Final transient error (retries == max_retries) flips the row to `failed`.

    `max_retries=0` collapses the first attempt INTO the exhaustion attempt, so
    a single eager run exercises the terminal-transient branch.
    """
    backend = MagicMock()
    backend.send.side_effect = TransientEmailError("dropped")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    monkeypatch.setattr(send_email_task, "max_retries", 0)

    email = _insert_queued(patched_session)
    with pytest.raises(TransientEmailError):
        send_email_task.delay(str(email.id)).get(timeout=5)

    patched_session.refresh(email)
    assert email.status == OutboundEmailStatus.FAILED
    assert email.error_type == "TransientEmailError"
    assert email.error_message == "dropped"
    assert email.attempts == 1


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_skips_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: Session,
) -> None:
    backend = MagicMock()
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    send_email_task.delay(str(uuid4())).get(timeout=5)

    backend.send.assert_not_called()


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_skips_already_sent_row(monkeypatch: pytest.MonkeyPatch, patched_session: Session) -> None:
    """At-least-once redelivery of a successfully-sent row must not re-send."""
    backend = MagicMock()
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = _insert_queued(patched_session)
    email.status = OutboundEmailStatus.SENT
    patched_session.commit()

    send_email_task.delay(str(email.id)).get(timeout=5)

    backend.send.assert_not_called()
    patched_session.refresh(email)
    assert email.attempts == 0  # early-return before the increment


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_skips_soft_deleted_row(monkeypatch: pytest.MonkeyPatch, patched_session: Session) -> None:
    backend = MagicMock()
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = _insert_queued(patched_session)
    email.soft_delete(None)
    patched_session.commit()

    send_email_task.delay(str(email.id)).get(timeout=5)

    backend.send.assert_not_called()
    patched_session.refresh(email)
    assert email.attempts == 0  # row read as missing → early return


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_merges_secret_context_into_render(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """Link-borne secrets ride the task signature, not the persisted row.

    The persisted context omits the token URL; the worker merges the
    `secret_context` arg back in only at render time, so the rendered body
    carries the link while the audit row never does.
    """
    backend = MagicMock()
    backend.send.return_value = None
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = OutboundEmail(
        template_name="platform_invitation",
        recipient="rcpt@example.com",
        context={
            "inviter_name": "Olive",
            "expires_at": "2026-01-01T00:00:00+00:00",
            "role_names": ["Red Teamer"],
        },
        backend="console",
        subject="You're invited",
    )
    patched_session.add(email)
    patched_session.commit()
    patched_session.refresh(email)

    secret = {"accept_url": "https://app.example.com/accept-invitation?token=secret-xyz"}
    send_email_task.delay(str(email.id), secret).get(timeout=5)

    patched_session.refresh(email)
    assert email.status == OutboundEmailStatus.SENT
    sent_message: EmailMessage = backend.send.call_args.args[0]
    assert "secret-xyz" in sent_message.body_text
    assert "secret-xyz" in sent_message.body_html
    assert "accept_url" not in email.context  # secret never persisted on the row


@pytest.mark.unit
def test_send_email_task_declares_retry_policy() -> None:
    assert send_email_task.max_retries == 5
    assert TransientEmailError in send_email_task.autoretry_for
    assert send_email_task.retry_backoff is True
    assert send_email_task.retry_jitter is True


@pytest.mark.unit
def test_send_email_task_is_discovered_by_celery_app() -> None:
    """Guard against regressions in `celery_app.autodiscover_tasks(...)`."""
    assert "app.core.email.tasks.send_email_task" in celery_app.tasks


def _insert_requester(session: Session) -> User:
    user = User(email=_REQUESTER_EMAIL, status=UserStatus.ACTIVE)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_notifies_the_requester_on_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    requester = _insert_requester(patched_session)

    email = _insert_queued(patched_session)
    email.requested_by_user_id = requester.id
    patched_session.commit()
    with pytest.raises(RuntimeError):
        send_email_task.delay(str(email.id)).get(timeout=5)

    notifications = patched_session.query(Notification).all()
    assert len(notifications) == 1
    assert notifications[0].user_id == requester.id
    assert "rcpt@example.com" in (notifications[0].description or "")


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_notifies_nobody_when_delivery_succeeds(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    backend = MagicMock()
    backend.send.return_value = "provider-msg-1"
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    requester = _insert_requester(patched_session)

    email = _insert_queued(patched_session)
    email.requested_by_user_id = requester.id
    patched_session.commit()
    send_email_task.delay(str(email.id)).get(timeout=5)

    assert patched_session.query(Notification).count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_failure_without_a_requester_notifies_nobody(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """A system-triggered mail (self-service re-issue, verification) has nobody to tell."""
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)

    email = _insert_queued(patched_session)
    with pytest.raises(RuntimeError):
        send_email_task.delay(str(email.id)).get(timeout=5)

    assert patched_session.query(Notification).count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_transient_with_budget_left_notifies_nobody(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """A retryable blip is not a delivery failure — the requester hears nothing yet."""
    backend = MagicMock()
    backend.send.side_effect = TransientEmailError("dropped")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    monkeypatch.setattr(send_email_task, "max_retries", 3)
    requester = _insert_requester(patched_session)

    email = _insert_queued(patched_session)
    email.requested_by_user_id = requester.id
    patched_session.commit()
    # Eager mode surfaces the scheduled retry itself, not the underlying error.
    with pytest.raises(Retry):
        send_email_task.apply(args=[str(email.id)], throw=True).get()

    patched_session.refresh(email)
    assert email.status == OutboundEmailStatus.QUEUED
    assert patched_session.query(Notification).count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_transient_at_retry_budget_notifies_the_requester(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """The other arm of the same branch: a spent retry budget is a delivery failure too.

    `max_retries=0` collapses the first attempt into the exhaustion attempt.
    """
    backend = MagicMock()
    backend.send.side_effect = TransientEmailError("dropped")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    monkeypatch.setattr(send_email_task, "max_retries", 0)
    requester = _insert_requester(patched_session)

    email = _insert_queued(patched_session)
    email.requested_by_user_id = requester.id
    patched_session.commit()
    with pytest.raises(TransientEmailError):
        send_email_task.delay(str(email.id)).get(timeout=5)

    notifications = patched_session.query(Notification).all()
    assert len(notifications) == 1
    assert notifications[0].user_id == requester.id


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_notifies_once_per_failed_batch(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """A provider outage across a bulk request is one event, not one per recipient."""
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    requester = _insert_requester(patched_session)
    batch_key = uuid4()

    for i in range(3):
        email = _insert_queued(patched_session)
        email.recipient = f"rcpt{i}@example.com"
        email.requested_by_user_id = requester.id
        email.batch_key = batch_key
        patched_session.commit()
        with pytest.raises(RuntimeError):
            send_email_task.delay(str(email.id)).get(timeout=5)

    notifications = patched_session.query(Notification).all()
    assert len(notifications) == 1
    assert "same bulk request" in (notifications[0].description or "")


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_notifies_per_row_without_a_batch_key(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """The dedupe is scoped to a batch — unrelated single sends each still report."""
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    requester = _insert_requester(patched_session)

    for i in range(2):
        email = _insert_queued(patched_session)
        email.recipient = f"solo{i}@example.com"
        email.requested_by_user_id = requester.id
        patched_session.commit()
        with pytest.raises(RuntimeError):
            send_email_task.delay(str(email.id)).get(timeout=5)

    assert patched_session.query(Notification).count() == 2


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_send_email_task_redelivery_of_a_failed_row_notifies_once(
    monkeypatch: pytest.MonkeyPatch, patched_session: Session
) -> None:
    """Celery acks late: a `failed` row can come back. The notification is per transition."""
    backend = MagicMock()
    backend.send.side_effect = RuntimeError("smtp 550 rejected")
    monkeypatch.setattr(tasks_module, "get_email_backend", lambda: backend)
    requester = _insert_requester(patched_session)

    email = _insert_queued(patched_session)
    email.requested_by_user_id = requester.id
    patched_session.commit()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            send_email_task.delay(str(email.id)).get(timeout=5)

    assert patched_session.query(Notification).count() == 1
