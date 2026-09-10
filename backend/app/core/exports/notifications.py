"""Side-effects of a finished export job: an audit row, an in-app notification, and an email.

Called from the export Celery task at each terminal transition (ready / failed /
timed-out). Every write is no-commit — `record_audit` and `create_notification`
flush, `send_email_best_effort` confines itself to a SAVEPOINT — so all three land
atomically with the task's own status-flip commit; a rolled-back finalize announces
nothing. The requester is the intended recipient (they asked for the export), so
there is no "don't notify the actor" guard.

Fields are passed explicitly rather than as an `ExportJob` so the reaper's
stuck-sweep can call this with the rows from its `UPDATE ... RETURNING` (which are
not full ORM instances) without a second read.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.models import User
from app.core.config import get_settings
from app.core.email import send_email_best_effort
from app.core.exports.naming import report_label
from app.core.exports.naming import resolve_scope_title
from app.core.logging import get_logger
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.services.notifications import create_notification

logger = get_logger(__name__)


def _scope(
    evaluation_id: UUID | None, evaluation_group_id: UUID | None
) -> tuple[NotificationObjectType | None, UUID | None]:
    if evaluation_id is not None:
        return NotificationObjectType.EVALUATION, evaluation_id
    if evaluation_group_id is not None:
        return NotificationObjectType.EVALUATION_GROUP, evaluation_group_id
    return None, None  # defensive: the model invariant is exactly one — degrade to a non-linked notice


async def emit_export_completion(  # noqa: PLR0913 — fields mirror the export row + outcome; a carrier object would just move the surface area
    session: AsyncSession,
    *,
    job_id: UUID,
    requested_by_id: UUID,
    evaluation_id: UUID | None,
    evaluation_group_id: UUID | None,
    template: str,
    export_format: str,
    created_at: datetime,
    succeeded: bool,
    error: str | None = None,
) -> None:
    """Record the audit row and, if the requester still exists, notify them in-app + by email.

    The audit row is written unconditionally (system actor — the completion is
    worker-driven, not an interactive action); the notification and email are
    skipped when the requester has since been soft-deleted.
    """
    object_type, object_id = _scope(evaluation_id, evaluation_group_id)
    title = await resolve_scope_title(session, evaluation_id=evaluation_id, evaluation_group_id=evaluation_group_id)
    label = report_label(scope_title=title, template=template, export_format=export_format, created_at=created_at)
    # Deep-link to the FE detail page (where the downloads list lives) — the export download endpoint
    # itself is bearer-gated, so an email/notification can't link the file bytes directly.
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    retention_hours = settings.export_job_ttl_seconds // 3600  # how long a ready file stays downloadable
    if evaluation_id is not None:
        target_url = f"{base}/evaluations/{evaluation_id}"
    elif evaluation_group_id is not None:
        target_url = f"{base}/evaluation-groups/{evaluation_group_id}"
    else:
        target_url = base

    context: dict[str, str] = {"requested_by_id": str(requested_by_id), "template": template, "format": export_format}
    if not succeeded and error is not None:
        context["error"] = error
    await record_audit(
        session,
        actor_id=None,
        actor_email=None,
        action=AuditAction.EXPORT_READY if succeeded else AuditAction.EXPORT_FAILED,
        object_type="export_job",
        object_id=job_id,
        context=context,
    )

    user = (await session.execute(User.live_select().where(col(User.id) == requested_by_id))).scalar_one_or_none()
    if user is None:
        # Audit still recorded above; the requester is gone, so there's no one to notify/email.
        logger.info("exports.notify.recipient_gone", job_id=str(job_id), requested_by_id=str(requested_by_id))
        return

    if succeeded:
        await create_notification(
            session,
            user_id=user.id,
            name="Export ready",
            description=f"{label} is ready to download. Available for {retention_hours} hours.",
            object_type=object_type,
            object_id=object_id,
        )
        await send_email_best_effort(
            session,
            "export_ready",
            user.email,
            {
                "recipient_name": user.display_name,
                "export_label": label,
                "target_url": target_url,
                "retention_hours": retention_hours,
            },
        )
    else:
        reason = error or "Unknown error."
        await create_notification(
            session,
            user_id=user.id,
            name="Export failed",
            description=f"{label} failed: {reason}",
            object_type=object_type,
            object_id=object_id,
        )
        await send_email_best_effort(
            session,
            "export_failed",
            user.email,
            {"recipient_name": user.display_name, "export_label": label, "error": reason, "target_url": target_url},
        )
