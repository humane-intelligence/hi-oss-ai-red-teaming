"""Service tests for the A/B orchestrator — SSE forwarded *and* the placeholder persisted."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from fastapi import status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import Usage
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.config import get_settings
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import TAG_CONTEXT_PARTIAL_EXTRA_KEY
from app.core.conversations.models import Message
from app.core.conversations.services.generation import MASKED_ERROR_DETAIL
from app.core.conversations.services.generation import persist_stream
from app.core.conversations.services.messages import open_turn
from tests.conftest import finalize_session_provider
from tests.core.conversations.services.conftest import persist_conversation

pytestmark = pytest.mark.integration


async def _placeholder(db_session: AsyncSession) -> Message:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    return next(m for m in opened.messages if m.role is MessageRole.ASSISTANT)


def _chunk(*, content: str | None = None, finish_reason: str | None = None, usage: Usage | None = None) -> ChatChunk:
    return ChatChunk(
        id="c",
        model="real-model-id",
        created=1,
        choices=[ChunkChoice(index=0, delta=ChatMessageDelta(content=content), finish_reason=finish_reason)],
        usage=usage,
    )


async def _stream(*chunks: ChatChunk, raises: Exception | None = None) -> AsyncIterator[ChatChunk]:
    for chunk in chunks:
        yield chunk
    if raises is not None:
        raise raises


async def test_persist_stream_seals_the_reply_in_a_protected_conversation(db_session: AsyncSession) -> None:
    # The assistant reply is the half a red-teamer actually reads back, and it is written here rather
    # than by `open_turn` — so this is the site where a missed seal would leave the transcript in the
    # clear while the row claims otherwise.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="here is how"), _chunk(finish_reason="stop", usage=Usage(total_tokens=5)))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=True,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.content_encrypted is True
    assert "here is how" not in placeholder.content
    assert unseal_content(placeholder.content, encrypted=True, settings=get_settings()) == "here is how"


async def test_persist_stream_finalizes_complete_with_content(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(
        _chunk(content="Hel"),
        _chunk(content="lo"),
        _chunk(finish_reason="stop", usage=Usage(total_tokens=5)),
    )

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=False,
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "delta", "done"]
    assert placeholder.status is MessageStatus.COMPLETE
    assert placeholder.content == "Hello"
    assert placeholder.extra["finish_reason"] == "stop"
    # Usage(total_tokens=5) serializes nested nulls (prompt/completion); the recursive
    # _drop_nulls must strip them — guard against a regression to a flat drop.
    assert placeholder.extra["usage"] == {"total_tokens": 5}


async def test_persist_stream_prefixes_stored_content(db_session: AsyncSession) -> None:
    # `continue` seeds the buffer with the prior partial via `prefix`: the persisted reply
    # is prefix + continuation, while the client receives only the new deltas.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="tail"), _chunk(finish_reason="stop"))

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=False,
            prefix="prior ",
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "done"]  # prefix not re-streamed to the client
    assert placeholder.status is MessageStatus.COMPLETE
    assert placeholder.content == "prior tail"  # stored = prefix + continuation


async def test_persist_stream_drops_all_null_usage(db_session: AsyncSession) -> None:
    # An all-None Usage() serializes nested nulls that collapse to {}; the key is
    # omitted entirely, not persisted as an empty dict.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="hi"), _chunk(finish_reason="stop", usage=Usage()))

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=False,
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "done"]
    assert placeholder.status is MessageStatus.COMPLETE
    assert "usage" not in placeholder.extra


async def test_persist_stream_omits_usage_when_provider_reports_none(db_session: AsyncSession) -> None:
    # A `done` with no usage serializes `usage: null`; it must not be persisted, so a
    # downstream reader of extra["usage"] sees a missing key, not None.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="hi"), _chunk(finish_reason="stop"))  # no usage reported

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=False,
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "done"]
    assert placeholder.status is MessageStatus.COMPLETE
    assert placeholder.extra["finish_reason"] == "stop"
    assert "usage" not in placeholder.extra  # null usage dropped, not stored


async def test_persist_stream_provider_error_finalizes_error(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="part"), raises=ProviderRateLimitError("slow down"))

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=False,
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "error"]
    assert placeholder.status is MessageStatus.ERROR
    assert placeholder.content == "part"
    assert placeholder.extra["error"]["status"] == status.HTTP_429_TOO_MANY_REQUESTS


async def test_persist_stream_masks_error_detail(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="part"), raises=ProviderRateLimitError("raw provider text naming the model"))

    events = [
        e
        async for e in persist_stream(
            chunks,
            placeholder.id,
            mask=True,
            session_provider=finalize_session_provider(db_session),
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    error_data = json.loads(events[-1]["data"])
    assert error_data["detail"] == MASKED_ERROR_DETAIL  # raw provider text never reaches a masked caller
    assert "detail" not in placeholder.extra["error"]  # and is never persisted


async def test_persist_stream_interrupted_on_disconnect(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    gen = persist_stream(
        _stream(_chunk(content="partial"), _chunk(content="never reached")),
        placeholder.id,
        mask=False,
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    )

    first = await gen.__anext__()  # first delta, mid-stream
    await gen.aclose()  # client disconnect before a terminal event
    await db_session.refresh(placeholder)

    assert first["event"] == "delta"
    assert placeholder.status is MessageStatus.INTERRUPTED
    assert placeholder.content == "partial"  # partial text preserved


async def test_persist_stream_finalizes_interrupted_on_real_cancellation(db_session: AsyncSession) -> None:
    # A real `asyncio.CancelledError` injected at the yield point (not the cooperative
    # `aclose()` above): the shielded finalize in the `finally` still runs and persists
    # the partial as `interrupted`.
    placeholder = await _placeholder(db_session)
    gen = persist_stream(
        _stream(_chunk(content="partial"), _chunk(content="more")),
        placeholder.id,
        mask=False,
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    )

    first = await gen.__anext__()  # first delta, mid-stream
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())  # real task cancellation
    await db_session.refresh(placeholder)

    assert first["event"] == "delta"
    assert placeholder.status is MessageStatus.INTERRUPTED
    assert placeholder.content == "partial"  # shielded finalize ran despite the cancel


@pytest.mark.parametrize("error", [SQLAlchemyError("db boom"), RuntimeError("bug")])
async def test_persist_stream_swallows_finalize_failure(db_session: AsyncSession, error: Exception) -> None:
    # Best-effort contract: a failing finalize must not break the SSE stream (re-raising
    # in persist_stream's finally would mask the real flow); the placeholder is left
    # `streaming` for the reaper. Covers both except branches (DB error vs unexpected bug).
    placeholder = await _placeholder(db_session)

    @asynccontextmanager
    async def failing_provider() -> AsyncIterator[AsyncSession]:
        raise error
        yield db_session  # unreachable — only makes this an async generator

    events = [
        e
        async for e in persist_stream(
            _stream(_chunk(content="hi"), _chunk(finish_reason="stop")),
            placeholder.id,
            mask=False,
            session_provider=failing_provider,
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "done"]  # stream completed, no raise
    assert placeholder.status is MessageStatus.STREAMING  # untouched, left for the reaper


async def test_persist_stream_swallows_finalize_db_error(db_session: AsyncSession) -> None:
    # The failure originates inside finalize_message's UPDATE on a live session (not the
    # provider context) — still swallowed; the placeholder is left `streaming`.
    placeholder = await _placeholder(db_session)

    class _ExplodingSession:
        async def execute(self, *args: object, **kwargs: object) -> None:
            raise SQLAlchemyError("update boom")

    @asynccontextmanager
    async def exploding_provider() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, _ExplodingSession())

    events = [
        e
        async for e in persist_stream(
            _stream(_chunk(content="hi"), _chunk(finish_reason="stop")),
            placeholder.id,
            mask=False,
            session_provider=exploding_provider,
            settings=get_settings(),
            protected=False,
        )
    ]
    await db_session.refresh(placeholder)

    assert [e["event"] for e in events] == ["delta", "done"]
    assert placeholder.status is MessageStatus.STREAMING


async def test_persist_stream_records_the_tag_context_it_was_given(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="hi"), _chunk(finish_reason="stop"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        tag_context={"env": "prod"},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.status is MessageStatus.COMPLETE
    assert placeholder.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}
    assert placeholder.extra["finish_reason"] == "stop"  # the record sits beside the telemetry, not instead of it


async def test_persist_stream_records_tag_context_beside_a_provider_error(db_session: AsyncSession) -> None:
    # A tag is a plausible cause of a refusal, so the diagnostic case must carry the context too.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="part"), raises=ProviderRateLimitError("slow down"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=True,
        tag_context={"env": "prod"},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.status is MessageStatus.ERROR
    assert placeholder.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}
    assert "detail" not in placeholder.extra["error"]  # masking invariant unchanged


async def test_persist_stream_records_tag_context_on_disconnect(db_session: AsyncSession) -> None:
    # `interrupted` is the absence of a terminal event, so a per-branch stamp would miss exactly the
    # partial output a reviewer squints at.
    placeholder = await _placeholder(db_session)
    gen = persist_stream(
        _stream(_chunk(content="partial"), _chunk(content="never reached")),
        placeholder.id,
        mask=False,
        tag_context={"env": "prod"},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    )

    await gen.__anext__()
    await gen.aclose()
    await db_session.refresh(placeholder)

    assert placeholder.status is MessageStatus.INTERRUPTED
    assert placeholder.content == "partial"
    assert placeholder.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}


@pytest.mark.parametrize("nothing_sent", [None, {}])
async def test_persist_stream_omits_tag_context_when_nothing_was_sent(
    db_session: AsyncSession, nothing_sent: dict[str, str] | None
) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="hi"), _chunk(finish_reason="stop"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        tag_context=nothing_sent,
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.extra == {"finish_reason": "stop"}  # no empty artefact, not merely an absent key


async def test_persist_stream_records_tag_context_alongside_a_continue_prefix(db_session: AsyncSession) -> None:
    # Guards the docstring's claim that a `continue` records the continuation's own tag_context on
    # top of the prior partial, rather than dropping one of the two in the same finalize.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="tail"), _chunk(finish_reason="stop"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        prefix="prior ",
        tag_context={"env": "prod"},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.content == "prior tail"
    assert placeholder.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}
    assert placeholder.extra[TAG_CONTEXT_PARTIAL_EXTRA_KEY] is True


async def test_persist_stream_omits_partial_flag_when_continue_records_no_tags(db_session: AsyncSession) -> None:
    # A `continue` with nothing sent stamps no record at all — the partial flag would be noise beside it.
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="tail"), _chunk(finish_reason="stop"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        prefix="prior ",
        tag_context={},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert TAG_CONTEXT_EXTRA_KEY not in placeholder.extra
    assert TAG_CONTEXT_PARTIAL_EXTRA_KEY not in placeholder.extra


async def test_persist_stream_omits_partial_flag_on_a_normal_send(db_session: AsyncSession) -> None:
    placeholder = await _placeholder(db_session)
    chunks = _stream(_chunk(content="hi"), _chunk(finish_reason="stop"))

    async for _ in persist_stream(
        chunks,
        placeholder.id,
        mask=False,
        tag_context={"env": "prod"},
        session_provider=finalize_session_provider(db_session),
        settings=get_settings(),
        protected=False,
    ):
        pass
    await db_session.refresh(placeholder)

    assert placeholder.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}
    assert TAG_CONTEXT_PARTIAL_EXTRA_KEY not in placeholder.extra
