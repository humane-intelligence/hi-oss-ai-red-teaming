"""Templated transactional email delivery — pluggable backend + Celery dispatch."""

from typing import Any
from uuid import UUID

from pydantic import EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email.models import OutboundEmail
from app.core.email.tasks import send_email_task
from app.core.email.templates import render_template
from app.core.email.templates import validate_context
from app.core.logging import get_logger
from app.core.notifications.services.notifications import create_notification

__all__ = ["send_email", "send_email_best_effort"]

logger = get_logger(__name__)


async def send_email(
    session: AsyncSession,
    template_name: str,
    to: EmailStr,
    context: dict[str, Any],
    *,
    secret_context: dict[str, Any] | None = None,
    requested_by_user_id: UUID | None = None,
    batch_key: UUID | None = None,
) -> UUID:
    """Send a templated email asynchronously; returns the `OutboundEmail` row id.

    Renders the template up front so template / context / recipient errors
    surface at the caller instead of in the worker; the rendered subject is
    stored on the row so queued mails have a non-empty subject in audit views.
    The task re-renders the bodies at delivery time. **Caller owns the
    commit** — the row only becomes visible once the surrounding transaction
    lands. A 1-second countdown on the task ensures the worker sees the
    committed row instead of racing the caller.

    `secret_context` carries link-borne bearer secrets (a token URL) that must
    *not* be persisted: those keys are validated alongside `context` but kept
    out of `OutboundEmail.context` and handed to the worker through the task
    signature instead (a transient broker message, gone once acked), merged
    back into the render context only at delivery time. The persisted
    `context` is therefore a non-secret subset that intentionally does not
    round-trip through `validate_context`.

    Args:
        session: Caller-owned async session; the audit row commits with it.
        template_name: Registered template key.
        to: Recipient address (kept out of the template context).
        context: Non-secret template variables, persisted on the audit row.
        secret_context: Link-borne secrets (e.g. `accept_url`); validated but
            never persisted. Defaults to no secrets.
        requested_by_user_id: Human who triggered this mail, if any — notified
            when delivery finally fails. Omit for system-triggered sends.
        batch_key: Correlates the mails of one bulk request, so a batch-wide
            delivery failure notifies its requester once instead of per row.

    Raises:
        KeyError: `template_name` is not in `TEMPLATES`.
        pydantic.ValidationError: the merged context does not match the template
            schema, or `to` is not a valid email address.
        jinja2.TemplateNotFound: template files missing on disk.
        jinja2.UndefinedError: template references a variable the schema
            doesn't supply (template / schema out of sync).
    """
    secret_keys = set(secret_context or {})
    validated = validate_context(template_name, {**context, **(secret_context or {})})
    message = render_template(template_name, to, validated)

    persisted = {key: value for key, value in validated.items() if key not in secret_keys}
    transient = {key: value for key, value in validated.items() if key in secret_keys}

    settings = get_settings()
    email = OutboundEmail(
        template_name=template_name,
        recipient=to,
        context=persisted,
        backend=settings.email_backend,
        subject=message.subject,
        requested_by_user_id=requested_by_user_id,
        batch_key=batch_key,
    )
    session.add(email)
    await session.flush()

    send_email_task.apply_async(args=[str(email.id), transient], countdown=1)
    return email.id


async def send_email_best_effort(
    session: AsyncSession,
    template_name: str,
    to: EmailStr,
    context: dict[str, Any],
    *,
    secret_context: dict[str, Any] | None = None,
    requested_by_user_id: UUID | None = None,
    batch_key: UUID | None = None,
) -> bool:
    """Send `template_name` inside a SAVEPOINT; log-and-swallow any failure.

    For mails that are post-conditions of a state transition that's already
    flushed (account activated, password changed, role assigned, ...): the
    user-visible outcome is already locked in, so a DB error inside `send_email`
    (e.g. `IntegrityError` on the audit row) must not poison the outer
    transaction and roll the activation back. `begin_nested()` confines the
    failure to the SAVEPOINT; `logger.exception` keeps it visible in structured
    logs — template / schema drift here is a production incident even though it
    does not break the user-visible flow.

    Callers whose mail is a *pre*-condition for the operation (invitation link,
    verification link, reset link) should call `send_email` directly so
    failures propagate and roll back the orphaned token row — unless they are
    dispatching one such mail per row of an already-committed bulk (the
    group-invitation and password-reset bulks). There the token rows are durable
    before dispatch starts, so there is nothing left to roll back and one broken
    message must not cost the rest of the batch its mail.

    Returns:
        Whether the mail was queued. A `batch_key` caller is dispatching in a
        loop and must aggregate: it gets `False` per lost mail and owns the
        single notification, because notifying here would write one row per
        failed recipient for what the operator experiences as one broken batch.
    """
    try:
        async with session.begin_nested():
            await send_email(
                session,
                template_name,
                to,
                context,
                secret_context=secret_context,
                requested_by_user_id=requested_by_user_id,
                batch_key=batch_key,
            )
    except Exception:
        # Recipient included so a partially-failed batch identifies WHOSE mail
        # was lost — the template alone can't.
        logger.exception("send_email_best_effort_failed", template=template_name, recipient=to)
        if requested_by_user_id is not None and batch_key is None:
            # The savepoint rolled back, so there is no `OutboundEmail` row and the worker
            # will never run — this is the only chance to tell the requester the mail died.
            # Its own savepoint, because the notify must not turn a swallowed mail failure
            # into a raise: the caller's state transition is already flushed, and a bulk
            # caller has committed rows whose request would otherwise 500.
            try:
                async with session.begin_nested():
                    await create_notification(
                        session,
                        user_id=requested_by_user_id,
                        name="Email could not be sent",
                        description=f"Preparing the message to {to} failed; nothing was queued.",
                    )
            except Exception:
                logger.exception("send_email_failure_notification_failed", recipient=to)
        return False
    return True
