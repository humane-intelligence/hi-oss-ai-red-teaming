"""Celery tasks for the conversations write-path.

Reaper: a hard process crash (SIGKILL/OOM/power loss) mid-stream kills the worker
before the detached finalize (Transaction B) runs, leaving an assistant placeholder
stuck in `streaming` forever. Handled interruptions (provider error, client
disconnect, timeout) finalize themselves to `error`/`interrupted` — only an
*unhandled* crash leaves the orphan this sweep exists to clean up.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from celery import shared_task
from sqlalchemy import cast
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import col

from app.core.config import get_settings
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.logging import get_logger
from app.workers.session import session_scope

logger = get_logger(__name__)

# Stamped into `extra` so a crash-reaped orphan is distinguishable from a handled
# client-disconnect — both land `interrupted`, but only the latter keeps partial text.
_REAPER_PROVENANCE = {"interrupted_by": "reaper"}


@shared_task(name="app.core.conversations.tasks.reap_orphaned_streaming_messages")
def reap_orphaned_streaming_messages() -> dict[str, int]:
    """Flip placeholders stuck in `streaming` past the TTL to `interrupted`.

    Idempotent: the guarded `WHERE status = 'streaming'` means a re-run touches only
    rows still stuck — anything already swept, or finalized normally, is left alone.
    The TTL guards against reaping an in-flight stream (a real generation finishes
    well within it). Scans via the partial `ix_messages_streaming` index. Content is
    not recoverable here (the buffer died with the process), so only `status` flips —
    plus an `interrupted_by: reaper` marker merged into `extra` for observability.

    No retry policy: a failed run needs no `autoretry_for` because the next scheduled
    beat tick is the recovery path, and the guarded `WHERE` makes that re-run a no-op
    for anything already swept.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.streaming_reap_ttl_seconds)
    with session_scope() as session:
        result = session.execute(
            update(Message)
            .where(col(Message.status) == MessageStatus.STREAMING, col(Message.created_at) < cutoff)
            .values(
                status=MessageStatus.INTERRUPTED,  # updated_at bumps via the column's onupdate=func.now()
                # JSONB `||` merge preserves any partial `extra`; the column is NOT NULL ('{}' default).
                extra=col(Message.extra).op("||")(cast(_REAPER_PROVENANCE, JSONB)),
            )
        )
        reaped: int = result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime
    if reaped:
        logger.info("conversations.reaper.swept", reaped=reaped)
    return {"reaped": reaped}
