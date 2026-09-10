"""Celery tasks for the media store.

Orphan GC: media is decoupled from its consumers by design (a referencing entity
stores the opaque key, no FK), so nothing cleans up an asset whose last reference
went away — a swapped cover or an abandoned upload leaves its row and blob forever.
This sweep soft-deletes live assets older than the grace period that no known
consumer references, then best-effort deletes their blobs (the soft-delete is
authoritative, mirroring the DELETE endpoint).

Two invariants:

- A reference counts regardless of the referencing row's liveness — flags/reviews
  can still reach a soft-deleted conversation's transcript, so its images survive.
- The consumer inventory is a convention, not a schema. Today: `Evaluation.cover_image`
  and `MessageImage.image_key`. A new column holding media keys MUST be added to the
  sweep below — otherwise the reaper deletes live images.

Accepted race: a reference committed mid-sweep is invisible to the reaper's SELECT, so
a graced-out asset can lose its blob just as it gains a consumer — a seconds-wide window
against a daily tick, and history degrades gracefully (the missing part is dropped with
a warning). If it ever hurts, key the cutoff to something an attach bumps.
"""

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from celery import shared_task
from sqlalchemy import select
from sqlmodel import col

from app.core.config import get_settings
from app.core.conversations.models import MessageImage
from app.core.evaluations.models import Evaluation
from app.core.logging import get_logger
from app.core.media.models import MediaAsset
from app.core.media.storage import get_media_storage
from app.workers.session import session_scope

logger = get_logger(__name__)


@shared_task(name="app.core.media.tasks.reap_orphaned_media")
def reap_orphaned_media() -> dict[str, int]:
    """Soft-delete unreferenced media past the grace period, then drop their blobs.

    Idempotent: `live_select` skips already-swept rows, and a failed blob delete only
    leaves a harmless orphaned file (the row is already gone — reads 404). No retry
    policy — the next beat tick is the recovery path.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(hours=settings.media_orphan_grace_hours)
    referenced_covers = select(col(Evaluation.cover_image)).where(col(Evaluation.cover_image).is_not(None))
    # No-op while image_key is NOT NULL, but a NULL in a NOT IN subquery would collapse the reaper to zero rows.
    referenced_attachments = select(col(MessageImage.image_key)).where(col(MessageImage.image_key).is_not(None))
    with session_scope() as session:
        orphans = list(
            session.execute(
                MediaAsset.live_select().where(
                    col(MediaAsset.created_at) < cutoff,
                    col(MediaAsset.key).not_in(referenced_covers),
                    col(MediaAsset.key).not_in(referenced_attachments),
                )
            )
            .scalars()
            .all()
        )
        keys = [asset.key for asset in orphans]
        for asset in orphans:
            asset.soft_delete(None)
    # Blob deletes after the commit: the soft-delete is authoritative (reads already
    # 404), so a crash between the two never resurrects an asset.
    failed = asyncio.run(_delete_blobs(keys)) if keys else 0
    if keys:
        logger.info("media.reaper.swept", reaped=len(keys), blob_failures=failed)
    return {"reaped": len(keys), "blob_failures": failed}


async def _delete_blobs(keys: list[str]) -> int:
    storage = get_media_storage()
    failed = 0
    for key in keys:
        try:
            await storage.delete(key)
        except Exception as exc:
            failed += 1
            logger.warning("media.reaper.blob_delete_failed", key=key, error=str(exc))
    return failed
