"""Rotation step: move sealed message text onto the active conversation key.

Run this **between** promoting a new `CONVERSATION_SECRETS_KEY` and dropping the previous one from
`CONVERSATION_SECRETS_KEY_RETIRED`. Until it has run, rows written under the retired key still need it
to be readable, so removing that key would make those transcripts unreadable — which is why a rotation
without this step is really a rotation of new writes only.

    make rewraptranscripts ARGS=--dry-run   # how many rows are still on an older key
    make rewraptranscripts                  # move them; exit 0 means nothing movable is left

The stop condition is the **real** run exiting 0, not the dry run reporting zero: a row sealed under a
key this deployment no longer holds stays in `pending` for ever, and dropping the retired key costs
nothing for it — it is already unopenable. A dry run counts those rows too and cannot tell them apart.

Safe to interrupt and re-run: work commits per chunk and the sweep walks by id, so a second pass
resumes instead of restarting. Rows sealed under a key this deployment no longer holds are logged with
their id, counted, and stepped over — one of them must not abort a rotation — and the run exits
non-zero while any remain, so a deploy step cannot read "finished" as "safe to drop the retired key".
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every table, so a flush can resolve cross-module FKs
from app.core.config import get_settings
from app.core.conversations.services.rewrap import DEFAULT_CHUNK_SIZE
from app.core.conversations.services.rewrap import rewrap_message_content
from app.core.conversations.services.rewrap import stale_message_count
from app.core.database import build_engine
from app.core.logging import configure_logging
from app.core.logging import get_logger

logger = get_logger(__name__)


def exit_code(pending: int, unreadable: int) -> int:
    """Map a finished run onto a process exit code.

    Non-zero only for work that a re-run could still do. Rows nobody holds a key for stay in `pending`
    for ever, so gating on `pending` alone would leave a deploy step failing after every rotation with
    no action left to take — and an operator who learns to ignore the code has lost the signal that
    matters. A separate function so the mapping is testable; `__main__` is not.
    """
    return 1 if pending > unreadable else 0


async def rewrap_transcripts(*, dry_run: bool = False, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[int, int]:
    """Re-wrap every sealed message, or just count; returns `(pending, unreadable)`.

    `pending` is the check an operator makes before dropping the retired key: rows in `messages` still
    on an older key or format. `unreadable` is how many of those no key opens, so no run will ever move
    them — the caller subtracts one from the other to decide the exit code.
    """
    settings = get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            if dry_run:
                pending = await stale_message_count(session, settings)
                logger.info("rewrap_transcripts.pending", messages=pending)
                return pending, 0
            result = await rewrap_message_content(session, settings, chunk_size=chunk_size)
            remaining = await stale_message_count(session, settings)
            logger.info(
                "rewrap_transcripts.done",
                moved=result.moved,
                unreadable=result.unreadable,
                skipped=result.skipped,
                remaining=remaining,
            )
            return remaining, result.unreadable
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-wrap sealed message text onto the active conversation key.")
    parser.add_argument("--dry-run", action="store_true", help="Only report how many rows are on an older key.")
    parser.add_argument(
        "--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Rows per transaction (default: %(default)s)."
    )
    arguments = parser.parse_args()
    if arguments.chunk_size < 1:
        parser.error("--chunk-size must be at least 1")
    pending, unreadable = asyncio.run(rewrap_transcripts(dry_run=arguments.dry_run, chunk_size=arguments.chunk_size))
    # A dry run is a report and always succeeds; only the real run gates a deploy step, and only on work
    # a re-run could still do.
    raise SystemExit(0 if arguments.dry_run else exit_code(pending, unreadable))
