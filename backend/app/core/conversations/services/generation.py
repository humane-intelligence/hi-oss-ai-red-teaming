"""A/B write-path orchestrator — stream the reply to the client *and* persist it.

The caller opens the turn (Transaction A) and releases the request connection,
then hands the resolved `ChatChunk` stream here. `persist_stream` forwards every
SSE event (reusing `stream_sse`, the single translator) while buffering the reply
text + terminal status, and finalizes the placeholder in a **detached** session
once the stream ends (Transaction B) — normally, on a provider error, or on a
client disconnect.

A still-`streaming` placeholder left by a cut/failed finalize is swept to
`interrupted` by the reaper (`app.core.conversations.tasks.reap_orphaned_streaming_messages`,
a periodic beat task), so the finalize here is best-effort.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.ai_gateway.chat import ChatChunk
from app.core.config import Settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import TAG_CONTEXT_PARTIAL_EXTRA_KEY
from app.core.conversations.services.messages import finalize_message
from app.core.conversations.streaming import stream_sse
from app.core.database import SessionProvider
from app.core.database import standalone_session
from app.core.logging import get_logger

logger = get_logger(__name__)

# Generic text shown to a masked caller instead of the raw provider error, which
# can name the real model. Stored `extra` never carries the raw text either.
MASKED_ERROR_DETAIL = "The model could not complete the request."


def _drop_nulls(data: dict[str, Any]) -> dict[str, Any]:
    """Drop null values, recursing into nested dicts and dropping ones that empty out.

    A wholly-null nested object (e.g. a `usage` with every field None) collapses to
    nothing rather than persisting an empty `{}` — so `extra["usage"]` is present iff
    it carries at least one real value, matching the wholly-absent-usage case.
    """
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _drop_nulls(value)
            if nested:
                cleaned[key] = nested
        else:
            cleaned[key] = value
    return cleaned


async def persist_stream(
    chunks: AsyncIterator[ChatChunk],
    placeholder_id: UUID,
    *,
    mask: bool,
    settings: Settings,
    protected: bool,
    prefix: str = "",
    tag_context: dict[str, str] | None = None,
    session_provider: SessionProvider = standalone_session,
) -> AsyncGenerator[dict[str, str]]:
    """Forward an SSE stream while persisting the assistant reply (A/B split, Transaction B).

    Yields the `delta` / `done` / `error` events from `stream_sse` unchanged (except
    a masked `error.detail`) and, on stream end, finalizes ``placeholder_id`` in a
    detached session with the buffered text and the terminal status: `complete` on
    `done`, `error` on a provider failure, `interrupted` if the stream is cut before
    a terminal event (client disconnect). ``extra`` records `finish_reason`/`usage`
    or the structured error — never the raw provider `detail`.

    Args:
        chunks: The already-resolved provider stream (from `dispatch_stream`).
        placeholder_id: The streaming assistant message to finalize.
        mask: Replace the `error` event's free-text `detail` (which can name the
            real model) with a generic message before forwarding. Required with no
            default — the caller must decide, so a masked, evaluation-bound caller
            can't leak the raw detail by omission.
        settings: Carries the key the reply is sealed under when the conversation is protected.
        protected: Whether the conversation's effective licence protects its content. Required with no
            default — a wrong `False` here stores a transcript in the clear that the licence says must
            not be.
        prefix: Text already shown to the client to prepend to the persisted reply
            (used by `continue` — the prior partial). It seeds the buffer so the
            stored content is prefix + continuation, but is **not** re-yielded; the
            client receives only the new deltas.
        tag_context: The tag map this dispatch carried — folded into the system message via
            `dispatch_*`'s `system_suffix`, never through `params` (rule 13) — recorded under
            ``extra["tag_context"]`` on whatever terminal state the stream reaches. Omitted from
            `extra` entirely when empty, so a reply generated with no tags carries no empty artefact.
            Record-only — the fold itself already happened at the call site. A `continue` records the
            continuation's context (flagged `extra["tag_context_partial"]`); the superseded message
            keeps its own.
        session_provider: Opens the detached finalize session; defaults to
            `standalone_session` (tests inject the test session).
    """
    buffer: list[str] = [prefix] if prefix else []
    status = MessageStatus.INTERRUPTED  # default until a terminal event is seen
    extra: dict[str, Any] = {}
    try:
        async for event in stream_sse(chunks):
            name = event["event"]
            if name == "delta":
                buffer.append(json.loads(event["data"])["content"])
            elif name == "done":
                status = MessageStatus.COMPLETE
                # Persist only non-null fields (recursively) so `extra` never holds a
                # null a reader would dereference.
                extra = _drop_nulls(json.loads(event["data"]))
            elif name == "error":
                status = MessageStatus.ERROR
                data = json.loads(event["data"])
                # Store only the structured fields — never the raw provider detail.
                extra = {"error": {key: data[key] for key in ("status", "title", "retryable")}}
                if mask:
                    data["detail"] = MASKED_ERROR_DETAIL
                    yield {**event, "data": json.dumps(data)}
                    continue
            yield event
    finally:
        # One stamp for all three terminal states: `done` and `error` set `extra` above, `interrupted`
        # is the absence of either, and only this point sees all three.
        if tag_context:
            extra = {**extra, TAG_CONTEXT_EXTRA_KEY: tag_context}
            if prefix:
                extra[TAG_CONTEXT_PARTIAL_EXTRA_KEY] = True
        # `shield` runs the finalize even when this task is cancelled. It is
        # best-effort, not awaited to completion — under cancellation it runs detached
        # (fire-and-forget), so an unfinished placeholder is left `streaming`. The
        # periodic reaper (`reap_orphaned_streaming_messages`) sweeps such orphans to
        # `interrupted` via the partial `status = 'streaming'` index.
        stored, encrypted = seal_content("".join(buffer), protected=protected, settings=settings)
        await asyncio.shield(_finalize(placeholder_id, stored, encrypted, status, extra, session_provider))


async def _finalize(
    placeholder_id: UUID,
    content: str,
    encrypted: bool,
    status: MessageStatus,
    extra: dict[str, Any],
    session_provider: SessionProvider,
) -> None:
    """Best-effort Transaction B.

    Runs in `persist_stream`'s `finally` (under `shield`), so it must never raise: an
    escaping exception would mask whatever is propagating through that `finally`. Both
    branches swallow and leave the placeholder `streaming` for the reaper, differing
    only in volume — a `SQLAlchemyError` (an expected transient failure) logs a quiet
    warning, anything else (a bug) logs a stacktrace.
    """
    try:
        async with session_provider() as session:
            await finalize_message(
                session, placeholder_id, content=content, status=status, encrypted=encrypted, extra=extra
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "conversations.finalize_failed",
            message_id=str(placeholder_id),
            status=status.value,
            error_class=type(exc).__name__,
        )
    except Exception as exc:
        logger.exception(
            "conversations.finalize_crashed",
            message_id=str(placeholder_id),
            status=status.value,
            error_class=type(exc).__name__,
        )
