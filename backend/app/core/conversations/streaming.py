"""Translate a normalized `ChatChunk` stream into SSE events.

Pure transport translation: chunk -> `delta` / `done` / `error` events, each
with a monotonic `id:` (groundwork for a future `Last-Event-ID` resume). Knows
nothing about turns, slots, or persistence. Its structured fields carry no model
or provider identity, so it can be shared with the masked, evaluation-bound
write-path endpoint — but the `error` event's `detail` is upstream provider text
(it can name the real model), which a masked caller must sanitise.
"""

from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from http import HTTPStatus

from fastapi import status
from pydantic import BaseModel

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.conversations.schemas import StreamDelta
from app.core.conversations.schemas import StreamDone
from app.core.conversations.schemas import StreamError

# (ProviderError subtype, HTTP status, retryable). retryable follows the taxonomy:
# transient upstream conditions (rate limit, timeout, unavailable) may succeed on
# retry; auth / bad-request / context-window will not.
_PROVIDER_ERROR_STATUS: tuple[tuple[type[ProviderError], int, bool], ...] = (
    (ProviderRateLimitError, status.HTTP_429_TOO_MANY_REQUESTS, True),
    (ProviderTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT, True),
    (ProviderUnavailableError, status.HTTP_502_BAD_GATEWAY, True),
    (ProviderContextWindowError, status.HTTP_400_BAD_REQUEST, False),
    (ProviderBadRequestError, status.HTTP_400_BAD_REQUEST, False),
    (ProviderAuthError, status.HTTP_502_BAD_GATEWAY, False),
)


def provider_error_to_status(exc: ProviderError) -> tuple[int, bool]:
    """Map a provider failure to ``(http_status, retryable)`` for an error event."""
    for exc_type, code, retryable in _PROVIDER_ERROR_STATUS:
        if isinstance(exc, exc_type):
            return code, retryable
    return status.HTTP_502_BAD_GATEWAY, False


def _event(name: str, seq: int, payload: BaseModel) -> dict[str, str]:
    return {"event": name, "id": str(seq), "data": payload.model_dump_json()}


async def stream_sse(chunks: AsyncIterator[ChatChunk]) -> AsyncGenerator[dict[str, str]]:
    """Yield SSE event dicts (`event` / `id` / `data`) from a normalized chunk stream.

    Emits a `delta` per non-empty content slice, one terminal `done` carrying
    `finish_reason` + `usage`, or an `error` event if a `ProviderError` surfaces
    mid-stream. `CancelledError` (client disconnect, raised inside by
    `EventSourceResponse`) is not caught — it propagates; the `finally` closes
    the upstream iterator so the provider stops generating.
    """
    seq = 0
    finish_reason: str | None = None
    usage = None
    try:
        async for chunk in chunks:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is not None:
                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason
                if choice.delta.content:
                    seq += 1
                    yield _event("delta", seq, StreamDelta(content=choice.delta.content))
            if chunk.usage is not None:
                usage = chunk.usage
        seq += 1
        yield _event("done", seq, StreamDone(finish_reason=finish_reason, usage=usage))
    except ProviderError as exc:
        code, retryable = provider_error_to_status(exc)
        seq += 1
        yield _event(
            "error",
            seq,
            StreamError(title=HTTPStatus(code).phrase, status=code, detail=str(exc), retryable=retryable),
        )
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            await aclose()
