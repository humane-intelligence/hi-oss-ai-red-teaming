"""Re-wrap sealed message text onto the current key and format.

The `<kid>:` prefix lets a rotation keep old rows readable through the retired slot, but only this
moves them onto the active key — and until every row is moved, the retired key cannot be dropped, so a
"rotation" that never runs this is really a rotation of new writes only. The predicate matches on
format as well as key, so a row written in a form this build cannot read is *surfaced* — counted
`unreadable` and logged with its id — rather than passed over silently. It is never moved: moving a
row means opening it first.

Deliberately not a Celery task: rotation is a supervised deploy step (promote the key, run this, check
the remaining count is zero, drop the retired key), not a cadence. `scripts/rewrap_transcripts.py` is
the entrypoint. It covers `messages.content` only — model credentials are a separate mechanism with a
separate key and no sweep, untouched by this one.
"""

from typing import NamedTuple
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import Settings
from app.core.conversations.content_crypto import ENVELOPE
from app.core.conversations.content_crypto import ContentDecryptError
from app.core.conversations.content_crypto import active_kid
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.models import Message
from app.core.logging import get_logger

logger = get_logger(__name__)

# Rows per transaction. Sized for transaction length, not CPU: re-wrapping one message costs ~7 µs
# (3.9 to open, 3.4 to seal), so a million rows is a few seconds of processor time.
DEFAULT_CHUNK_SIZE = 500

# How many unreadable rows are named individually before the log falls back to the final count. The
# common cause of a large number is one mistake — a promoted key with the previous one left out of the
# retired slot — and naming every row of a million-row table ships hundreds of megabytes to say it.
UNREADABLE_LOG_CAP = 20


class RewrapResult(NamedTuple):
    """What one sweep did — three outcomes, because each needs a different action.

    `unreadable` is not a failure of the sweep: those rows were sealed under a key this deployment no
    longer holds (an abandoned key, a dump restored elsewhere), so no re-wrap can reach them. They are
    counted rather than raised so one such row cannot abort a rotation, and reported rather than skipped
    silently so the operator learns the remaining count will never fall to zero on its own.

    `skipped` is a row another sweep re-wrapped between this one's read and its write. Counted so
    `moved + unreadable + skipped` accounts for every row examined — otherwise a second sweep running
    concurrently reports `moved=0` and reads identically to "there was nothing to do".
    """

    moved: int
    unreadable: int
    skipped: int


def _stale_predicate(settings: Settings) -> ColumnElement[bool]:
    """Sealed rows not on both the active key and the current format.

    Expressed in SQL rather than by testing each row in Python, so the sweep can ask the database for
    only the work that is left — which is also what makes the loop resumable and the count cheap.
    """
    # Matches on key **and** format, so a value in an older form is swept even under the active key.
    return col(Message.content_encrypted).is_(True) & ~col(Message.content).startswith(
        f"{active_kid(settings)}:{ENVELOPE}"
    )


async def stale_message_count(session: AsyncSession, settings: Settings) -> int:
    """How many sealed messages are still on an older key or the older format.

    The number an operator checks before dropping the retired key: zero means nothing left depends on it.
    """
    return (
        await session.execute(select(func.count()).select_from(Message).where(_stale_predicate(settings)))
    ).scalar_one()


async def _rewrap_row(session: AsyncSession, message_id: UUID, stale: str, settings: Settings) -> bool:
    """Re-seal one row under the active key; return whether the write landed.

    The UPDATE is guarded on the exact ciphertext that was read, so whoever wrote last wins instead of
    being overwritten with a re-wrap of a value read earlier. In practice the only writer that can race
    it is **another sweep**: every live write path is guarded `WHERE status = 'streaming'`, and a
    streaming placeholder is always empty and unsealed, so it never matches this sweep's predicate.

    `updated_at` is pinned to its stored value: re-wrapping is not an edit of the message, and letting
    the ORM's `onupdate` fire would restamp the entire protected corpus in one rotation.
    """
    opened = unseal_content(stale, encrypted=True, settings=settings)
    fresh, encrypted = seal_content(opened, protected=True, settings=settings)
    result = await session.execute(
        update(Message)
        .where(col(Message.id) == message_id, col(Message.content) == stale)
        # The discriminator is written alongside the value so the two cannot disagree, even in the
        # case `seal_content` declines to seal.
        .values(content=fresh, content_encrypted=encrypted, updated_at=Message.updated_at)
    )
    return result.rowcount > 0  # ty: ignore[unresolved-attribute]  # CursorResult at runtime


async def rewrap_message_content(
    session: AsyncSession, settings: Settings, *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> RewrapResult:
    """Move every sealed message onto the active conversation key; report what moved and what could not.

    Chunked and committed per chunk, so an interrupted run leaves durable progress and re-running
    resumes rather than restarts. Paged by an id cursor rather than by re-querying from the start: a row
    that cannot be opened stays stale, so a restart-from-the-top loop would hand back the same row for
    ever. Ordering by id makes the walk finite whatever each row's outcome is.
    """
    moved = 0
    unreadable = 0
    skipped = 0
    cursor: UUID | None = None
    while True:
        query = select(col(Message.id), col(Message.content)).where(_stale_predicate(settings))
        if cursor is not None:
            query = query.where(col(Message.id) > cursor)
        rows = (await session.execute(query.order_by(col(Message.id)).limit(chunk_size))).all()
        if not rows:
            break
        for message_id, stale in rows:
            try:
                if await _rewrap_row(session, message_id, stale, settings):
                    moved += 1
                else:
                    skipped += 1
            except ContentDecryptError as exc:
                # Counted, logged with the row, and stepped over: aborting here would leave the rotation
                # half-done, and the next run would stop at the same row.
                unreadable += 1
                if unreadable <= UNREADABLE_LOG_CAP:
                    logger.warning("conversations.rewrap.unreadable", message_id=str(message_id), reason=str(exc))
        cursor = rows[-1][0]
        await session.commit()
    if unreadable > UNREADABLE_LOG_CAP:
        logger.warning("conversations.rewrap.unreadable_truncated", listed=UNREADABLE_LOG_CAP, total=unreadable)
    logger.info(
        "conversations.rewrap.done",
        moved=moved,
        unreadable=unreadable,
        skipped=skipped,
        active_kid=active_kid(settings),
    )
    return RewrapResult(moved=moved, unreadable=unreadable, skipped=skipped)
