"""API tests for `POST /api/v1/chat/stream`.

Auth is faked by overriding `current_user` (`as_user`); the provider is faked by
monkeypatching the dispatch layer's default `ModelProvider`, so no network and
no real litellm call. Cancellation semantics are covered at the service level in
`tests/core/conversations/test_streaming.py`.
"""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import Usage
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.services.ai_models import create_model
from app.core.auth.models import User
from app.core.auth.services.users import create_user
from app.core.config import get_settings
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role

pytestmark = pytest.mark.integration

_ALIAS = "target"
_BODY = {"model_alias": _ALIAS, "messages": [{"role": "user", "content": "hi"}]}


async def _user(db: AsyncSession, permissions: list[str]) -> User:
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[await make_role(db, permissions)])


async def _seed_model(db: AsyncSession) -> None:
    await create_model(
        db,
        get_settings(),
        name="Target",
        model_alias=_ALIAS,
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )


def _chunk(*, content: str | None = None, finish_reason: str | None = None, usage: Usage | None = None) -> ChatChunk:
    return ChatChunk(
        id="c",
        model="provider-real-id",
        created=1,
        choices=[ChunkChoice(index=0, delta=ChatMessageDelta(content=content), finish_reason=finish_reason)],
        usage=usage,
    )


class _FakeProvider:
    def __init__(self, chunks: list[ChatChunk], *, raise_at_end: Exception | None = None) -> None:
        self._chunks = chunks
        self._raise_at_end = raise_at_end

    async def stream(self, **_kwargs: object) -> AsyncIterator[ChatChunk]:
        for chunk in self._chunks:
            yield chunk
        if self._raise_at_end is not None:
            raise self._raise_at_end


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("app.core.ai_gateway.dispatch._DEFAULT_PROVIDER", provider)


def _parse_sse(text: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        event: dict[str, str] = {}
        for line in block.split("\n"):
            field, _, value = line.partition(":")
            if field in ("event", "data", "id"):
                event[field] = value.lstrip()
        if event:
            events.append(event)
    return events


async def test_stream_returns_deltas_then_done(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:participate"])
    await _seed_model(db_session)
    _patch_provider(
        monkeypatch,
        _FakeProvider(
            [_chunk(content="Hel"), _chunk(content="lo"), _chunk(finish_reason="stop", usage=Usage(total_tokens=5))]
        ),
    )

    with as_user(user):
        response = await async_client_with_db.post("/api/v1/chat/stream", json=_BODY)

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert [e["event"] for e in events] == ["delta", "delta", "done"]
    assert "".join(json.loads(e["data"])["content"] for e in events[:2]) == "Hello"
    assert json.loads(events[-1]["data"])["finish_reason"] == "stop"


async def test_stream_unknown_alias_returns_problem_json(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["conversations:participate"])

    with as_user(user):
        response = await async_client_with_db.post(
            "/api/v1/chat/stream", json={"model_alias": "missing", "messages": [{"role": "user", "content": "hi"}]}
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == status.HTTP_404_NOT_FOUND


async def test_stream_without_permission_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["evaluations:read"])

    with as_user(user):
        response = await async_client_with_db.post("/api/v1/chat/stream", json=_BODY)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


async def test_stream_rejects_remote_image_url(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, ["conversations:participate"])
    body = {
        "model_alias": _ALIAS,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest/meta-data/"}}],
            }
        ],
    }

    with as_user(user):
        response = await async_client_with_db.post("/api/v1/chat/stream", json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"] == "application/problem+json"


async def test_stream_midstream_error_becomes_error_event(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:participate"])
    await _seed_model(db_session)
    _patch_provider(
        monkeypatch, _FakeProvider([_chunk(content="part")], raise_at_end=ProviderRateLimitError("slow down"))
    )

    with as_user(user):
        response = await async_client_with_db.post("/api/v1/chat/stream", json=_BODY)

    assert response.status_code == status.HTTP_200_OK
    events = _parse_sse(response.text)
    assert [e["event"] for e in events] == ["delta", "error"]
    error = json.loads(events[-1]["data"])
    assert error["status"] == status.HTTP_429_TOO_MANY_REQUESTS
    assert error["retryable"] is True


async def test_stream_releases_db_session(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:participate"])
    await _seed_model(db_session)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")]))

    closed = {"value": False}
    real_close = db_session.close

    async def _tracking_close() -> None:
        closed["value"] = True
        await real_close()

    monkeypatch.setattr(db_session, "close", _tracking_close)

    with as_user(user):
        response = await async_client_with_db.post("/api/v1/chat/stream", json=_BODY)

    assert response.status_code == status.HTTP_200_OK
    assert closed["value"] is True
