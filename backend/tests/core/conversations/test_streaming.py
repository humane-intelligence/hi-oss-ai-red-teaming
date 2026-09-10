"""Unit tests for the SSE translation service — pure chunk-stream in, event dicts out."""

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import status

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import Usage
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.conversations.streaming import provider_error_to_status
from app.core.conversations.streaming import stream_sse

# A real provider-reported model id; events must never echo it (masking invariant).
_REAL_MODEL = "gpt-4o-2024-08-06"


def _chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
    choices: list[ChunkChoice] | None = None,
) -> ChatChunk:
    if choices is None:
        choices = [ChunkChoice(index=0, delta=ChatMessageDelta(content=content), finish_reason=finish_reason)]
    return ChatChunk(id="c", model=_REAL_MODEL, created=1, choices=choices, usage=usage)


async def _from(*chunks: ChatChunk, raises: Exception | None = None) -> AsyncIterator[ChatChunk]:
    for chunk in chunks:
        yield chunk
    if raises is not None:
        raise raises


async def _collect(events: AsyncIterator[dict[str, str]]) -> list[dict[str, str]]:
    return [event async for event in events]


@pytest.mark.unit
async def test_stream_sse_emits_deltas_then_done() -> None:
    chunks = _from(
        _chunk(content="Hel"),
        _chunk(content="lo"),
        _chunk(finish_reason="stop", usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)),
    )

    events = await _collect(stream_sse(chunks))

    assert [e["event"] for e in events] == ["delta", "delta", "done"]
    assert [json.loads(e["data"])["content"] for e in events[:2]] == ["Hel", "lo"]
    done = json.loads(events[-1]["data"])
    assert done["finish_reason"] == "stop"
    assert done["usage"]["total_tokens"] == 5


@pytest.mark.unit
async def test_stream_sse_ids_are_monotonic() -> None:
    chunks = _from(_chunk(content="a"), _chunk(content="b"), _chunk(finish_reason="stop"))

    events = await _collect(stream_sse(chunks))

    assert [e["id"] for e in events] == ["1", "2", "3"]


@pytest.mark.unit
async def test_stream_sse_carries_usage_from_empty_choices_terminal_chunk() -> None:
    # OpenAI's include_usage chunk has empty choices and only usage.
    chunks = _from(
        _chunk(content="hi", finish_reason=None),
        _chunk(finish_reason="stop"),
        _chunk(choices=[], usage=Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14)),
    )

    events = await _collect(stream_sse(chunks))

    assert [e["event"] for e in events] == ["delta", "done"]
    assert json.loads(events[-1]["data"])["usage"]["total_tokens"] == 14


@pytest.mark.unit
async def test_stream_sse_maps_midstream_error_to_error_event() -> None:
    chunks = _from(_chunk(content="partial"), raises=ProviderRateLimitError("slow down"))

    events = await _collect(stream_sse(chunks))

    assert [e["event"] for e in events] == ["delta", "error"]
    err = json.loads(events[-1]["data"])
    assert err["status"] == status.HTTP_429_TOO_MANY_REQUESTS
    assert err["retryable"] is True
    assert err["detail"] == "slow down"


@pytest.mark.unit
async def test_stream_sse_never_emits_model_identity() -> None:
    chunks = _from(_chunk(content="hi"), _chunk(finish_reason="stop"), raises=None)

    events = await _collect(stream_sse(chunks))

    for event in events:
        assert _REAL_MODEL not in event["data"]
        assert "model" not in json.loads(event["data"])


@pytest.mark.unit
async def test_stream_sse_closes_upstream_on_disconnect() -> None:
    state = {"closed": False}

    async def _tracked() -> AsyncIterator[ChatChunk]:
        try:
            yield _chunk(content="a")
            yield _chunk(content="b")
        finally:
            state["closed"] = True

    events = stream_sse(_tracked())
    await events.__anext__()  # first delta, mid-stream

    await events.aclose()  # client disconnect -> EventSourceResponse closes the generator

    assert state["closed"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_retryable"),
    [
        (ProviderRateLimitError("x"), status.HTTP_429_TOO_MANY_REQUESTS, True),
        (ProviderBadRequestError("x"), status.HTTP_400_BAD_REQUEST, False),
        (ProviderError("x"), status.HTTP_502_BAD_GATEWAY, False),
    ],
)
def test_provider_error_to_status(exc: ProviderError, expected_status: int, expected_retryable: bool) -> None:
    assert provider_error_to_status(exc) == (expected_status, expected_retryable)
