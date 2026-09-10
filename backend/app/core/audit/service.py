"""Audit-log service — record one event, and read the log back (admin).

`record_audit` is the single write helper: mutating handlers call it inside their
`@transactional` body (so the audit row commits atomically with the action);
`AuditAccessMiddleware` calls it for access events in its own session. It reads
`request_id` ambiently from `structlog.contextvars` — the caller never threads it.

`before`/`after` must be **curated** dicts — never whole rows, credentials or tokens
(OWASP). Identifying fields (email, names) are included deliberately as attribution and
retained under the (deferred) audit-retention / right-to-erasure policy — the trail is
not PII-free, it is secret-free. Access events pass `before=after=None`.
"""

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.filters import AuditLogFilters
from app.core.audit.filters import AuditLogOrderBy
from app.core.audit.models import AuditLog
from app.core.ordering import apply_order_by
from app.core.pagination import paginate


def changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """Reduce two full snapshots to only the keys whose value changed.

    Returns ``(before_subset, after_subset)`` over the changed keys — so an audit
    row records *what changed* (``{"first_name": "Ada"}`` → ``{"first_name": "Bo"}``),
    not the whole object. Both empty when nothing changed. Keys are the union of both
    sides, so a key dropped from ``after`` still reads as a change (``after`` → ``None``).
    """
    keys = [k for k in {**before, **after} if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


def _current_request_id() -> str | None:
    """Read the request id bound by `LoggingMiddleware` into structlog contextvars."""
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


async def record_audit(  # noqa: PLR0913 — keyword-only args mirror the audit row's column set; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    actor_id: UUID | None,
    actor_email: str | None,
    action: AuditAction,
    object_type: str | None = None,
    object_id: UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    context: dict | None = None,
) -> AuditLog:
    """Append one audit row (does not commit — the caller owns the transaction).

    On a `@transactional` handler this is atomic with the action (a rollback drops
    the audit row too). ``before``/``after`` are curated, non-secret snapshots for
    mutations; leave them ``None`` for access events.

    Returns:
        The flushed (uncommitted) row, so a handler that manually manages its
        transaction can delete it to compensate an action it later rolls back —
        `object_id` is a bare UUID with no FK cascade, so cleanup is the caller's.
    """
    row = AuditLog(
        actor_id=actor_id,
        actor_email=actor_email,
        action=action.value,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
        context=context or {},
        request_id=_current_request_id(),
    )
    session.add(row)
    await session.flush()
    return row


async def list_audit_logs(
    session: AsyncSession,
    *,
    filters: AuditLogFilters,
    order_by: AuditLogOrderBy,
    limit: int,
    offset: int,
) -> tuple[list[AuditLog], int]:
    """Return one page of audit rows matching ``filters`` (append-only → plain select)."""
    statement = select(AuditLog)
    if filters.actor_id is not None:
        statement = statement.where(col(AuditLog.actor_id) == filters.actor_id)
    if filters.action is not None:
        statement = statement.where(col(AuditLog.action) == filters.action)
    if filters.object_type is not None:
        statement = statement.where(col(AuditLog.object_type) == filters.object_type)
    if filters.object_id is not None:
        statement = statement.where(col(AuditLog.object_id) == filters.object_id)
    if filters.created_from is not None:
        statement = statement.where(col(AuditLog.created_at) >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(col(AuditLog.created_at) <= filters.created_to)
    statement = apply_order_by(statement, AuditLog, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)
