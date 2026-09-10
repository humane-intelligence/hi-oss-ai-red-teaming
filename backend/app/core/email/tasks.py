"""Celery task that renders and sends a queued outbound_emails row.

Lifecycle: row is created in `queued` state by `send_email()` in `__init__.py`.
This task picks it up, renders the template against the row's `context`, hands
to the configured backend, and flips the row to `sent` or `failed`.

Idempotency: row lookup is by stable id; multiple worker pickups update
the same row. Actual delivery is NOT idempotent — duplicate emails on
retry are acceptable. Full dedupe needs an Idempotency-Key column and a
unique constraint; we'll add it once a real caller demands it.
"""

from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlmodel import col

from app.core.email.backends import TransientEmailError
from app.core.email.backends import get_email_backend
from app.core.email.models import OutboundEmail
from app.core.email.models import OutboundEmailStatus
from app.core.email.templates import render_template
from app.core.logging import get_logger
from app.core.notifications.models import Notification
from app.workers.session import session_scope

logger = get_logger(__name__)


def _notify_requester(session: Session, email: OutboundEmail) -> None:
    """Tell whoever asked for this mail that it will not arrive.

    Written on the same sync session as the failure checkpoint, so the notification
    lands with the `failed` status or not at all. `create_notification` is async and
    unusable from a worker, hence the direct insert.

    One notification per *batch*, not per row: a provider outage during a 100-row
    invite would otherwise bury the operator under 100 near-identical rows for what
    is one event. The first failure of a batch reports it; the rest stay silent and
    are found in `outbound_emails`. Two rows failing in the same instant can still
    both notify — each runs in its own transaction and neither sees the other's
    commit — which is a rare duplicate, not a flood.
    """
    if email.requested_by_user_id is None:
        return
    if email.batch_key is not None and _batch_already_failed(session, email):
        return
    session.add(
        Notification(
            user_id=email.requested_by_user_id,
            name="Email could not be delivered",
            description=(
                f"Delivery to {email.recipient} failed after {email.attempts} attempt(s)."
                if email.batch_key is None
                else (
                    f"Delivery to {email.recipient} failed after {email.attempts} attempt(s). "
                    "Other messages from the same bulk request may have failed too — check the email log."
                )
            ),
        )
    )


def _batch_already_failed(session: Session, email: OutboundEmail) -> bool:
    """Whether another mail of this batch is already marked `failed`.

    Excludes `email` itself: the caller sets its status before this runs, and the
    autoflush on this query would otherwise make every row see itself.
    """
    return (
        session.execute(
            select(col(OutboundEmail.id))
            .where(
                col(OutboundEmail.batch_key) == email.batch_key,
                col(OutboundEmail.status) == OutboundEmailStatus.FAILED,
                col(OutboundEmail.id) != email.id,
            )
            .limit(1),
        ).first()
        is not None
    )


@shared_task(
    name="app.core.email.tasks.send_email_task",
    bind=True,
    autoretry_for=(TransientEmailError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(self, email_id: str, secret_context: dict[str, Any] | None = None) -> None:
    """Render the templated email and ship it via the configured backend.

    `secret_context` holds link-borne secrets (a token URL) kept out of the
    persisted `OutboundEmail.context`; they ride the task signature instead and
    are merged into the render context here, never touching the audit row.
    Celery re-enqueues with the same args on retry, so the secret survives
    redelivery.
    """
    with session_scope() as session:
        email = session.get(OutboundEmail, UUID(email_id))
        if email is None:
            # Caller forgot to commit the enqueueing transaction (transient).
            logger.warning("email.task.row_missing", email_id=email_id)
            return
        if email.is_deleted:
            # Admin took action — terminal; retry/wait won't help.
            logger.warning("email.task.row_soft_deleted", email_id=email_id)
            return

        # Celery is at-least-once: an ack lost after a successful send would
        # otherwise redeliver and double-send. Treat `sent` as terminal.
        if email.status == OutboundEmailStatus.SENT:
            logger.info("email.task.already_sent", email_id=email_id)
            return

        # Celery acks late, so a row already marked `failed` can be redelivered and fail
        # again; the notification must fire on the transition only, not per attempt.
        was_failed = email.status is OutboundEmailStatus.FAILED
        email.attempts += 1
        email.celery_task_id = self.request.id

        try:
            render_context = {**email.context, **(secret_context or {})}
            message = render_template(email.template_name, email.recipient, render_context)
            backend = get_email_backend()
            email.provider_message_id = backend.send(message)
        except Exception as exc:
            email.error_type = type(exc).__name__
            email.error_message = str(exc)
            # Transient errors only stay `queued` while the retry budget has
            # room; once exhausted (or for any non-transient error) the row
            # is terminal so it doesn't sit on `queued` forever.
            is_transient = isinstance(exc, TransientEmailError)
            if not is_transient or self.request.retries >= self.max_retries:
                email.status = OutboundEmailStatus.FAILED
                if not was_failed:
                    _notify_requester(session, email)
            session.commit()  # deliberate checkpoint — persist attempt + error before raise → Celery retry
            raise

        email.status = OutboundEmailStatus.SENT
        email.sent_at = datetime.now(UTC)
        email.error_type = None
        email.error_message = None
