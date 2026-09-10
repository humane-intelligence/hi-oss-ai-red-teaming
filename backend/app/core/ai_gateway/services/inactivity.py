"""Inactivity sweep: alert admins about warmup-enabled models nobody is using.

A scale-to-zero endpoint is warmed on every conversation open, so one can stay hot —
and billable — long after the last real message. Warmups therefore do not count as
usage (see `dispatch._stamp_warmup`); a model goes quiet the moment messages stop,
which is exactly the state worth flagging.

Alerts fire once per episode: `inactivity_alerted_at` is stamped under a guarded
UPDATE, and any real traffic — or restoring a knob that silenced it (enable, warm-up,
threshold, undelete) — clears it, re-arming the next one. Disabling is left to a human; this
only tells them it is worth doing.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import func
from sqlmodel import select
from sqlmodel.sql.expression import SelectOfScalar

from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.email import send_email_best_effort
from app.core.logging import get_logger
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.services.notifications import create_notification

logger = get_logger(__name__)

_SECONDS_PER_HOUR = 3600


def _inactive_predicate(now: datetime) -> list[ColumnElement[bool]]:
    idle_since = func.coalesce(col(AiModel.last_used_at), col(AiModel.created_at))
    return [
        col(AiModel.warmup_enabled).is_(True),
        col(AiModel.disabled_at).is_(None),
        col(AiModel.inactivity_alerted_at).is_(None),
        col(AiModel.inactivity_alert_hours).is_not(None),
        func.extract("epoch", now - idle_since) >= col(AiModel.inactivity_alert_hours) * _SECONDS_PER_HOUR,
    ]


async def find_inactive_models(session: AsyncSession, *, now: datetime) -> list[AiModel]:
    """Warmup-enabled live models whose quiet period has elapsed and not yet alerted.

    A model that never carried traffic is measured from `created_at`, so a registered
    but forgotten endpoint alerts too.
    """
    return list((await session.execute(AiModel.live_select().where(*_inactive_predicate(now)))).scalars().all())


async def _claim(session: AsyncSession, model_id: UUID, *, now: datetime) -> bool:
    """Stamp the alert, returning False if the row is no longer alertable.

    Repeats the whole inactive predicate, not just the `IS NULL` stamp guard: traffic
    landing (or a disable/soft-delete) between the sweep's SELECT and this UPDATE must
    lose the row — a stamp CAS alone would alert on a just-used model *and* leave the
    stamp set, silently swallowing the next genuine episode. Also what makes the sweep
    safe under `acks_late` redelivery and overlapping beat ticks: one run wins the row.
    """
    result = await session.execute(
        AiModel.live_update()
        .where(col(AiModel.id) == model_id, *_inactive_predicate(now))
        # `updated_at` pinned for the same reason as the dispatch stamps: a system write.
        .values(inactivity_alerted_at=now, updated_at=col(AiModel.updated_at))
    )
    return bool(result.rowcount)  # ty: ignore[unresolved-attribute]  # CursorResult at runtime


def _permission_holders(permission: Permission) -> SelectOfScalar[UUID]:
    role_ids = select(col(Role.id)).where(
        col(Role.deleted_at).is_(None),
        col(Role.is_active).is_(True),
        col(Role.permissions).contains([permission.value]),
    )
    return select(col(UserRole.user_id)).where(col(UserRole.role_id).in_(role_ids))


async def _recipients(session: AsyncSession) -> list[User]:
    """Active users who can actually act on the alert — holders of `models:update` + `models:read`.

    Keyed on the capabilities rather than the `admin` role name (the `annotators` precedent):
    `models:update` is delegable, so an operator-defined role holding it must get the alert too.
    `models:read` is required alongside because the alert deep-links to the model page, which
    is gated on it — an update-only holder would land on NotAuthorized. Each capability may
    come from a different role: the requirement is per user, not per role.
    """
    statement = User.live_select().where(
        col(User.status) == UserStatus.ACTIVE,
        col(User.id).in_(_permission_holders(Permission.MODELS_UPDATE)),
        col(User.id).in_(_permission_holders(Permission.MODELS_READ)),
    )
    return list((await session.execute(statement)).scalars().all())


class _Candidate(NamedTuple):
    model_id: UUID
    name: str
    last_used_at: datetime | None
    last_warmup_at: datetime | None
    idle_since: datetime
    alert_hours: int


async def alert_inactive_models(session: AsyncSession) -> int:
    """Alert admins about every model that has gone quiet, and report how many fired.

    Each alert is claimed before it is announced, so a redelivered or overlapping run
    re-sends nothing.

    **Commits as it goes, rather than leaving it to the caller.** `send_email` queues its
    Celery task with a 1-second countdown, and the worker drops the mail for good if the
    `outbound_emails` row is not committed by the time it looks (`email.task.row_missing`,
    no retry). One commit at the end would put a whole sweep's fan-out — every model times
    every recipient — inside that window. Committing the claim before any mail is queued
    also makes "exactly one run wins the row" survive a crash mid-fan-out.
    """
    now = datetime.now(UTC)
    candidates = await find_inactive_models(session, now=now)
    if not candidates:
        return 0

    recipients = await _recipients(session)
    if not recipients:
        logger.warning("ai_gateway.inactivity.no_recipients", candidates=len(candidates))
        return 0

    # Snapshot plain values before the first commit: commit and rollback both expire every
    # loaded instance, and a lazy refresh on an AsyncSession raises MissingGreenlet.
    rows = [
        _Candidate(
            m.id,
            m.name,
            m.last_used_at,
            m.last_warmup_at,
            m.last_used_at or m.created_at,
            m.inactivity_alert_hours or 0,
        )
        for m in candidates
    ]
    recipient_rows = [(r.id, r.email, r.display_name) for r in recipients]

    base = get_settings().frontend_base_url.rstrip("/")
    alerted = 0
    for row in rows:
        if not await _claim(session, row.model_id, now=now):
            continue
        await session.commit()
        alerted += 1
        idle_hours = int((now - row.idle_since).total_seconds() // _SECONDS_PER_HOUR)
        # "Still warmed" is judged against the alerted quiet period itself (not a fixed
        # span), so the copy can never contradict the model's own threshold.
        quiet_since = now - timedelta(hours=row.alert_hours)
        still_warmed = row.last_warmup_at is not None and row.last_warmup_at >= quiet_since
        target_url = f"{base}/ai-models/{row.model_id}"
        for recipient_id, email, display_name in recipient_rows:
            # The claim is already committed, so a raise here would leave the episode
            # permanently half-alerted — one bad recipient must not starve the rest.
            try:
                await create_notification(
                    session,
                    user_id=recipient_id,
                    name="Model idle",
                    description=(
                        f"'{row.name}' has had no messages for {idle_hours} hours"
                        + (
                            " but is still being warmed, so it is burning endpoint time for nobody."
                            if still_warmed
                            else ", and nothing is warming it either."
                        )
                        + " Disable it if nobody needs it."
                    ),
                    object_type=NotificationObjectType.AI_MODEL,
                    object_id=row.model_id,
                )
                await send_email_best_effort(
                    session,
                    "model_inactivity_alert",
                    email,
                    {
                        "recipient_name": display_name,
                        "model_name": row.name,
                        "idle_hours": idle_hours,
                        "last_used_at": row.last_used_at,
                        "last_warmup_at": row.last_warmup_at,
                        "still_warmed": still_warmed,
                        "target_url": target_url,
                    },
                )
                await session.commit()
            except Exception:
                logger.exception(
                    "ai_gateway.inactivity.recipient_failed",
                    model_id=str(row.model_id),
                    recipient_id=str(recipient_id),
                )
                await session.rollback()
        logger.info("ai_gateway.inactivity.alerted", model_id=str(row.model_id), idle_hours=idle_hours)
    return alerted
