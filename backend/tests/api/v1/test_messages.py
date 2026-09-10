"""API tests for the conversation message SSE endpoints (send / regenerate / continue).

Auth is faked via `as_user`; the provider via monkeypatching the dispatch layer's
default `ModelProvider` (no network). The detached finalize (`persist_stream` →
`standalone_session`) needs the lifespan engine, absent in tests — so it raises and is
swallowed by `_finalize`. Hence most of these tests assert the **streamed events** and
that **Transaction A persisted** (turn + user message + placeholder); the finalized
reply content is otherwise covered at the service level in
`tests/core/conversations/services/`. A test that needs the finalized row itself (the
tag-context record) opts in via `_finalize_into`, which routes Transaction B onto the
test session instead.

`_keep_open` neutralises the handler's `await db.close()` so the test session stays
usable for the post-request persistence query and for multi-request idempotency cases.
"""

import inspect
import io
import json
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.api.v1 import messages as messages_router
from app.api.v1.messages import _replay_events
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.chat import TextContentPart
from app.core.ai_gateway.chat import Usage
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import User
from app.core.auth.services.users import create_user
from app.core.config import Settings
from app.core.config import get_settings
from app.core.conversations.content_crypto import ContentDecryptError
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import MessageImage
from app.core.conversations.models import Turn
from app.core.conversations.services.generation import MASKED_ERROR_DETAIL
from app.core.conversations.services.messages import finalize_message
from app.core.conversations.tags import TAG_CONTEXT_PREAMBLE
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.exceptions import NotFoundError
from app.core.media.models import MediaAsset
from app.core.media.services.images import clear_data_url_cache
from app.core.media.services.images import create_image
from app.core.media.storage import get_media_storage
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role
from tests.conftest import finalize_session_provider
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _user(db: AsyncSession, permissions: list[str]) -> User:
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[await make_role(db, permissions)])


async def _conversation(  # noqa: PLR0913 — one keyword per cascade layer the tests set up independently
    db: AsyncSession,
    user: User,
    *,
    mask: bool = True,
    input_modalities: list[Modality] | None = None,
    output_modalities: list[Modality] | None = None,
    model_params: dict[str, object] | None = None,
    assignment_params: dict[str, object] | None = None,
    conversation_params: dict[str, object] | None = None,
    advanced_params_disabled: bool = False,
) -> Conversation:
    group = await persist_evaluation_group(db, access_level=EvaluationGroupAccessLevel.PUBLIC, created_by_id=user.id)
    evaluation = Evaluation(
        title="E", description="d", evaluation_group_id=group.id, created_by_id=user.id, mask_models_enabled=mask
    )
    db.add(evaluation)
    await db.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(
        name=alias,
        model_alias=alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id=f"{alias}-id",
        input_modalities=input_modalities or [Modality.TEXT],
        output_modalities=output_modalities or [Modality.TEXT],
        parameters=model_params or {},
        advanced_params_disabled=advanced_params_disabled,
    )
    db.add(model)
    await db.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, parameters=assignment_params or {})
    db.add(assignment)
    await db.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db.add(scenario)
    await db.flush()
    conversation_group = ConversationGroup(
        user_id=user.id, evaluation_id=evaluation.id, name="group", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=user.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
        parameters=conversation_params or {},
    )
    db.add(conversation)
    await db.flush()
    await db.refresh(conversation)
    return conversation


def _url(conversation: Conversation, suffix: str = "") -> str:
    return f"/api/v1/evaluations/{conversation.evaluation_id}/conversations/{conversation.id}/messages{suffix}"


async def _attached_keys(db: AsyncSession, message_id: object) -> list[str]:
    statement = (
        select(col(MessageImage.image_key))
        .where(col(MessageImage.message_id) == message_id)
        .order_by(col(MessageImage.position))
    )
    return list((await db.execute(statement)).scalars().all())


async def _image_asset(db: AsyncSession, user: User, *, is_private: bool = True) -> MediaAsset:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    asset = await create_image(db, raw=buffer.getvalue(), created_by_id=user.id, is_private=is_private)
    await db.flush()
    return asset


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
        self.calls = 0
        self.received_messages: list[ChatMessage] | None = None  # the prompt the provider was handed
        self.received_params: dict[str, object] | None = None  # the merged inference params

    async def stream(
        self,
        *,
        messages: list[ChatMessage] | None = None,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[ChatChunk]:
        self.calls += 1
        self.received_messages = list(messages) if messages is not None else None
        self.received_params = params
        for chunk in self._chunks:
            yield chunk
        if self._raise_at_end is not None:
            raise self._raise_at_end


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("app.core.ai_gateway.dispatch._DEFAULT_PROVIDER", provider)


def _keep_open(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> None:
    """Neutralise the handler's `await db.close()` so the test session stays queryable."""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(db, "close", _noop)


def _finalize_into(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> None:
    """Route Transaction B onto the test session, so what finalize writes is observable here."""
    real = messages_router.persist_stream

    def _with_test_session(
        chunks: AsyncIterator[ChatChunk],
        placeholder_id: UUID,
        *,
        mask: bool,
        settings: Settings,
        protected: bool,
        prefix: str = "",
        tag_context: dict[str, str] | None = None,
    ) -> AsyncGenerator[dict[str, str]]:
        return real(
            chunks,
            placeholder_id,
            mask=mask,
            settings=settings,
            protected=protected,
            prefix=prefix,
            tag_context=tag_context,
            session_provider=finalize_session_provider(db),
        )

    monkeypatch.setattr(messages_router, "persist_stream", _with_test_session)


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


async def _turns(db: AsyncSession, conversation: Conversation) -> list[Turn]:
    return list((await db.execute(select(Turn).where(Turn.conversation_id == conversation.id))).scalars().all())


async def _messages(db: AsyncSession, turn: Turn) -> list[Message]:
    return list((await db.execute(select(Message).where(Message.turn_id == turn.id))).scalars().all())


async def _refresh_after_finalize(db: AsyncSession, conversation: Conversation) -> None:
    """Re-sync the identity map after `_finalize_into` routed the detached finalize onto this session.

    That finalize writes through a raw UPDATE issued from underneath the ORM, so `conversation` (and
    anything loaded through it) is a stale identity-mapped instance until expired — reading it
    otherwise returns pre-finalize data, and touching a lazy attribute raises `MissingGreenlet`.
    `refresh` rather than a bare `expire`, since `expire_all` also expires `.id` itself.
    """
    db.expire_all()
    await db.refresh(conversation)


async def test_send_streams_and_persists_turn(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="Hel"), _chunk(content="lo"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [e["event"] for e in _parse_sse(response.text)] == ["delta", "delta", "done"]
    assert provider.received_messages is not None
    assert [(m.role, m.content) for m in provider.received_messages] == [("user", "hi")]  # the prompt the model saw

    turns = await _turns(db_session, conversation)
    assert len(turns) == 1
    messages = await _messages(db_session, turns[0])
    user_msg = next(m for m in messages if m.role is MessageRole.USER)
    assistant = next(m for m in messages if m.role is MessageRole.ASSISTANT)
    assert user_msg.content == "hi"
    assert user_msg.status is MessageStatus.COMPLETE
    assert assistant.status is MessageStatus.STREAMING  # detached finalize needs the lifespan engine


async def test_send_with_image_attaches_multimodal_content_part(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    asset = await _image_asset(db_session, user)
    provider = _FakeProvider([_chunk(content="a cat"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "what is this?", "image_keys": [asset.key]}
        )

    assert response.status_code == status.HTTP_200_OK
    assert provider.received_messages is not None
    content = provider.received_messages[0].content
    assert isinstance(content, list)
    text_part, image_part = content
    assert isinstance(text_part, TextContentPart)
    assert text_part.text == "what is this?"
    assert isinstance(image_part, ImageContentPart)
    assert image_part.image_url.url.startswith("data:image/png;base64,")

    turns = await _turns(db_session, conversation)
    user_msg = next(m for m in await _messages(db_session, turns[0]) if m.role is MessageRole.USER)
    assert await _attached_keys(db_session, user_msg.id) == [asset.key]


async def test_send_with_multiple_images_keeps_attachment_order(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    first = await _image_asset(db_session, user)
    second = await _image_asset(db_session, user)
    provider = _FakeProvider([_chunk(content="two cats"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "compare these", "image_keys": [first.key, second.key]}
        )

    assert response.status_code == status.HTTP_200_OK
    assert provider.received_messages is not None
    content = provider.received_messages[0].content
    assert isinstance(content, list)
    assert [type(part) for part in content] == [TextContentPart, ImageContentPart, ImageContentPart]

    turns = await _turns(db_session, conversation)
    user_msg = next(m for m in await _messages(db_session, turns[0]) if m.role is MessageRole.USER)
    assert await _attached_keys(db_session, user_msg.id) == [first.key, second.key]


async def test_send_rejects_more_than_five_images(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation),
            json={"content": "too many", "image_keys": [f"2026/01/01/{i}.png" for i in range(6)]},
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_send_rejects_foreign_private_image(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # Another user's upload reads as unknown — the same 400 as a bad key, no existence leak.
    owner = await _user(db_session, ["conversations:update"])
    other = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, other, input_modalities=[Modality.TEXT, Modality.IMAGE])
    foreign = await _image_asset(db_session, owner)

    with as_user(other):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "image_keys": [foreign.key]}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Unknown image_key" in response.json()["detail"]


async def test_send_rejects_public_image_attachment(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Conversation attachments must be private by construction, not FE convention —
    # a public attachment would stay fetchable by bare key.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    asset = await _image_asset(db_session, user, is_private=False)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "image_keys": [asset.key]}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "is_private" in response.json()["detail"]


async def test_send_with_unknown_image_key_is_rejected(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    _patch_provider(monkeypatch, _FakeProvider([_chunk(finish_reason="stop")]))

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "image_keys": ["2026/01/01/deadbeef.png"]}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")
    assert await _turns(db_session, conversation) == []  # rejected before the turn is opened


async def test_send_image_to_non_vision_model_is_rejected(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model isn't vision-capable, so the dispatch gate refuses the image — a 400,
    # not a leak to the provider.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=False)  # input_modalities defaults to text alone
    asset = await _image_asset(db_session, user)
    provider = _FakeProvider([_chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "what is this?", "image_keys": [asset.key]}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert provider.calls == 0


async def test_regenerate_after_image_deleted_drops_attachment_and_streams(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # attach → send → the image's blob vanishes → regenerate proceeds WITHOUT the image
    # (graceful degradation), rather than 404ing the whole request.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    asset = await _image_asset(db_session, user)
    provider = _FakeProvider([_chunk(content="a cat"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(
            _url(conversation), json={"content": "what is this?", "image_keys": [asset.key]}
        )
    turn0 = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant.id, content="a cat", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()
    await get_media_storage().delete(asset.key)  # blob gone; the persisted attachment now dangles
    clear_data_url_cache()  # the send cached the data URL — drop it so the dangling read is real
    provider.calls = 0  # ignore the send's dispatch — count only the regenerate

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/regenerate"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert provider.calls == 1  # generation proceeded; the missing image was dropped from history
    # The rebuilt prompt carries the text but no image part (the vanished attachment was skipped).
    assert provider.received_messages is not None
    content = provider.received_messages[0].content
    assert [type(part) for part in content] == [TextContentPart]
    assert content[0].text == "what is this?"


async def test_send_after_prior_image_deleted_drops_attachment_and_streams(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prior turn's attachment blob vanishes (out-of-band delete / reaper); the next send
    # rebuilds history over it and proceeds WITHOUT the image (graceful degradation), rather
    # than 404ing. Send routes the vanished-blob read through the same history rebuild as
    # regenerate — cover it here too.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, input_modalities=[Modality.TEXT, Modality.IMAGE])
    asset = await _image_asset(db_session, user)
    provider = _FakeProvider([_chunk(content="a cat"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(
            _url(conversation), json={"content": "what is this?", "image_keys": [asset.key]}
        )
    turn0 = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant.id, content="a cat", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()
    await get_media_storage().delete(asset.key)  # blob gone; the prior attachment now dangles
    clear_data_url_cache()  # the first send cached the data URL — drop it so the dangling read is real
    provider.calls = 0  # ignore the first send's dispatch — count only the second

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "still there?"})

    assert response.status_code == status.HTTP_200_OK
    assert provider.calls == 1  # generation proceeded; the missing prior image was dropped from history
    # The rebuilt prompt keeps the prior turn's text but no image part (the vanished attachment was skipped).
    assert provider.received_messages is not None
    assert [type(part) for part in provider.received_messages[0].content] == [TextContentPart]
    assert provider.received_messages[0].content[0].text == "what is this?"


async def test_resolution_failure_settles_placeholder_not_streaming(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model-resolution failure AFTER Transaction A opened the turn must settle the placeholder
    # as `error`, never strand it `streaming` for the reaper to sweep.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    async def _unavailable(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object], bool]:
        raise NotFoundError("Model assignment is unavailable.")

    monkeypatch.setattr("app.api.v1.messages.resolve_dispatch_target", _unavailable)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert provider.calls == 0
    turn0 = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    settled = (await db_session.execute(select(Message).where(Message.id == assistant.id))).scalar_one()
    assert settled.status is MessageStatus.ERROR
    assert "error" in settled.extra  # the masked-safe {status, title, retryable}, not stranded empty
    assert TAG_CONTEXT_EXTRA_KEY not in settled.extra  # the fold never ran, so there is nothing to record


async def test_tag_policy_read_failure_settles_placeholder_not_streaming(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tagging-policy read sits between Transaction A and the provider open, so a parent that
    # vanished in that window has to settle the placeholder like a resolution failure does — not
    # strand it `streaming` until the reaper's TTL. The soft delete rides on a collaborator so it
    # lands inside that window; the code under test is where the fold sits relative to the guard.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.tags = {"env": "prod"}  # a non-empty map, or the fold returns before reading the policy
    await db_session.flush()
    provider = _FakeProvider([_chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    resolve_for_real = messages_router.resolve_dispatch_target

    async def _resolve_then_lose_the_evaluation(
        session: AsyncSession, conv: Conversation, *args: object, **kwargs: object
    ) -> tuple[str, dict[str, object], bool]:
        resolved = await resolve_for_real(session, conv, *args, **kwargs)
        evaluation = await session.get(Evaluation, conv.evaluation_id)
        assert evaluation is not None
        evaluation.soft_delete(None)
        await session.flush()
        return resolved

    monkeypatch.setattr("app.api.v1.messages.resolve_dispatch_target", _resolve_then_lose_the_evaluation)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert provider.calls == 0
    turn0 = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    settled = (await db_session.execute(select(Message).where(Message.id == assistant.id))).scalar_one()
    assert settled.status is MessageStatus.ERROR
    assert "error" in settled.extra  # the masked-safe {status, title, retryable}, not stranded empty
    assert TAG_CONTEXT_EXTRA_KEY not in settled.extra  # the policy read failed before a map was folded


async def test_resume_appends_with_prior_turn_in_context(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming a conversation: a 2nd send hands the model the 1st turn's completed reply as context."""
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="a2"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "q1"})
    # The harness can't run the detached finalize, so complete turn 0's reply explicitly.
    turn0 = (await _turns(db_session, conversation))[0]
    assistant0 = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant0.id, content="a1", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "q2"})

    assert response.status_code == status.HTTP_200_OK
    assert [(m.role, m.content) for m in provider.received_messages or []] == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2"),
    ]


async def test_send_with_params_merges_into_dispatch(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "params": {"temperature": 0.2}}
        )

    assert response.status_code == status.HTTP_200_OK
    assert provider.received_params == {"temperature": 0.2}  # per-request override reached dispatch


async def test_send_with_params_merges_over_full_cascade(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-request params win over the resolved model→assignment→conversation cascade; unset knobs are inherited."""
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(
        db_session,
        user,
        model_params={"temperature": 0.1, "max_tokens": 1024},
        assignment_params={"temperature": 0.5, "top_p": 0.9},
        conversation_params={"temperature": 0.9},
    )
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "params": {"temperature": 0.2}}
        )

    assert response.status_code == status.HTTP_200_OK
    # temperature: per-request wins; top_p: from assignment; max_tokens: from model.
    assert provider.received_params == {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1024}


async def test_send_midstream_error_becomes_error_event(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="part")], raise_at_end=ProviderRateLimitError("slow")))

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    events = _parse_sse(response.text)
    assert [e["event"] for e in events] == ["delta", "error"]
    assert json.loads(events[-1]["data"])["status"] == status.HTTP_429_TOO_MANY_REQUESTS


async def test_send_without_permission_returns_403(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, ["conversations:read"])
    conversation = await _conversation(db_session, user)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_send_unknown_conversation_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    url = f"/api/v1/evaluations/{conversation.evaluation_id}/conversations/{uuid4()}/messages"

    with as_user(user):
        response = await async_client_with_db.post(url, json={"content": "hi"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_send_rejects_empty_content(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": ""})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT  # UserMessageIn.content min_length=1


async def test_send_rejects_whitespace_only_content(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "   \n\t"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT  # min_length passes; strip-validator rejects


async def test_idempotent_replay_creates_no_second_turn(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    body = {"content": "hi", "client_message_id": str(uuid4())}

    with as_user(user):
        first = await async_client_with_db.post(_url(conversation), json=body)
        second = await async_client_with_db.post(_url(conversation), json=body)

    assert first.status_code == second.status_code == status.HTTP_200_OK
    assert provider.calls == 1  # replay does not re-generate
    assert len(await _turns(db_session, conversation)) == 1  # no duplicate turn
    done = next(e for e in _parse_sse(second.text) if e["event"] == "done")
    assert json.loads(done["data"])["finish_reason"] == "streaming"  # in-flight replay isn't a blank success


async def test_send_in_a_protected_conversation_seals_the_reply(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route is the only place that tells the streaming finalize whether to seal. Asserting the raw
    # column (not the projection) is what makes a wrong `protected` here visible at all.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.content_protected = True
    await db_session.commit()
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="secret reply"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    await _refresh_after_finalize(db_session, conversation)
    assistant = await _assistant_of(db_session, conversation)
    stored = (
        await db_session.execute(text("SELECT content FROM messages WHERE id = :id"), {"id": assistant.id})
    ).scalar_one()
    assert "secret reply" not in stored
    assert assistant.content_encrypted is True
    assert unseal_content(stored, encrypted=True, settings=get_settings()) == "secret reply"


async def test_send_settles_the_placeholder_when_history_cannot_be_unsealed(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row sealed under a key this deployment no longer holds (a rotation that dropped the retired
    # key, a dump restored elsewhere) fails while history is built — after Transaction A committed the
    # placeholder. Left unhandled it strands the row `streaming` until the reaper's TTL.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.content_protected = True
    await db_session.flush()
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi"})
    foreign = get_settings().model_copy(update={"conversation_secrets_key": SecretStr("k" * 40)})
    stored, encrypted = seal_content("unreadable", protected=True, settings=foreign)
    prior = await _assistant_of(db_session, conversation)
    await finalize_message(db_session, prior.id, content=stored, status=MessageStatus.COMPLETE, encrypted=encrypted)
    await db_session.commit()

    # The client re-raises unhandled server errors, so the raise itself is the 500 the caller sees;
    # matched on the message rather than the class, which the module also raises on tampering.
    with pytest.raises(ContentDecryptError, match="matches no configured key"), as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "again"})

    await _refresh_after_finalize(db_session, conversation)
    latest = (await _turns(db_session, conversation))[-1]
    placeholder = next(m for m in await _messages(db_session, latest) if m.role is MessageRole.ASSISTANT)
    assert placeholder.status is MessageStatus.ERROR


async def _assistant_of(db: AsyncSession, conversation: Conversation) -> Message:
    turns = await _turns(db, conversation)
    return next(m for m in await _messages(db, turns[0]) if m.role is MessageRole.ASSISTANT)


async def test_replay_of_completed_reply_restreams_content(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())
    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
    assistant = await _assistant_of(db_session, conversation)
    await finalize_message(db_session, assistant.id, content="final", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        replay = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    events = _parse_sse(replay.text)
    assert [e["event"] for e in events] == ["delta", "done"]
    assert json.loads(events[0]["data"])["content"] == "final"
    assert json.loads(events[1]["data"])["finish_reason"] == "replayed"


async def test_replay_under_protection_restreams_plaintext(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replay is a read of a stored reply, so it needs the same unsealing as every other read path —
    # otherwise a retried send answers with the ciphertext and the chat pane renders it as the model's reply.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.content_protected = True
    await db_session.flush()
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())
    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
    assistant = await _assistant_of(db_session, conversation)
    stored, encrypted = seal_content("final", protected=True, settings=get_settings())
    await finalize_message(db_session, assistant.id, content=stored, status=MessageStatus.COMPLETE, encrypted=encrypted)
    await db_session.commit()

    with as_user(user):
        replay = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    events = _parse_sse(replay.text)
    assert json.loads(events[0]["data"])["content"] == "final"


async def test_replay_of_an_unreadable_reply_fails_before_the_stream_opens(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unsealing inside the SSE generator would run after the response headers are sent, so the caller
    # would get 200 and a body that stops mid-stream with no `error` event — indistinguishable from a
    # dropped connection. Resolved before the response is built, it is an ordinary error instead.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.content_protected = True
    await db_session.flush()
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())
    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
    assistant = await _assistant_of(db_session, conversation)
    foreign = get_settings().model_copy(update={"conversation_secrets_key": SecretStr("k" * 40)})
    stored, encrypted = seal_content("final", protected=True, settings=foreign)
    await finalize_message(db_session, assistant.id, content=stored, status=MessageStatus.COMPLETE, encrypted=encrypted)
    await db_session.commit()

    with pytest.raises(ContentDecryptError, match="matches no configured key"), as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    # Where the failure lands is what matters, and no client assertion reaches it: the ASGI transport
    # surfaces an exception raised inside the SSE body exactly like one raised before the response, so
    # both placements look identical from here (checked by mutation). Pinned on the shape instead —
    # a generator with no settings has nothing to unseal with, so the resolution cannot move back in.
    assert "settings" not in inspect.signature(_replay_events).parameters


async def test_replay_of_errored_reply_emits_error_event(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A retry of a key whose original failed must re-report the failure, not a blank success.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())
    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
    await finalize_message(
        db_session,
        (await _assistant_of(db_session, conversation)).id,
        content="",
        status=MessageStatus.ERROR,
        extra={"error": {"status": 502, "title": "Bad Gateway", "retryable": True}},
        encrypted=False,
    )
    await db_session.commit()

    with as_user(user):
        replay = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    events = _parse_sse(replay.text)
    assert [e["event"] for e in events] == ["error"]
    data = json.loads(events[0]["data"])
    assert data["status"] == 502
    assert data["retryable"] is True
    assert data["detail"] == MASKED_ERROR_DETAIL  # raw provider text is never re-exposed on replay


async def test_client_message_id_collision_across_conversations_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    first_conv = await _conversation(db_session, user)
    other_conv = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())

    with as_user(user):
        first = await async_client_with_db.post(_url(first_conv), json={"content": "hi", "client_message_id": cmid})
        collision = await async_client_with_db.post(_url(other_conv), json={"content": "hi", "client_message_id": cmid})

    assert first.status_code == status.HTTP_200_OK
    assert collision.status_code == status.HTTP_409_CONFLICT


async def _open_assistant(client: AsyncClient, db: AsyncSession, user: User, conversation: Conversation) -> Message:
    with as_user(user):
        await client.post(_url(conversation), json={"content": "hi"})
    turns = await _turns(db, conversation)
    return next(m for m in await _messages(db, turns[0]) if m.role is MessageRole.ASSISTANT)


async def test_regenerate_streams(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="redo"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)
    await finalize_message(db_session, assistant.id, content="orig", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/regenerate"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert [e["event"] for e in _parse_sse(response.text)] == ["delta", "done"]
    # The superseded "orig" reply is dropped from context; only the user prompt remains.
    assert [(m.role, m.content) for m in provider.received_messages or []] == [("user", "hi")]


async def test_continue_streams(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="more"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)
    await finalize_message(db_session, assistant.id, content="orig", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/continue"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert [e["event"] for e in _parse_sse(response.text)] == ["delta", "done"]
    # The prior reply is appended as context so the model continues from it.
    assert [(m.role, m.content) for m in provider.received_messages or []] == [("user", "hi"), ("assistant", "orig")]


async def test_continue_under_protection_carries_the_plaintext_prior(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `continue` is the one flow that reads a stored reply back as input. Handing it the raw column
    # would prompt the model with the ciphertext and re-seal it into the new reply, so the prior text
    # would be unreachable through every read path at once.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.content_protected = True
    await db_session.flush()
    provider = _FakeProvider([_chunk(content="more"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)
    stored, encrypted = seal_content("orig", protected=True, settings=get_settings())
    await finalize_message(db_session, assistant.id, content=stored, status=MessageStatus.COMPLETE, encrypted=encrypted)
    await db_session.commit()
    # Wired only now: the first send has to leave its placeholder streaming so the sealed "orig" above
    # is the prior reply, while the continuation's own finalize still has to land on this session.
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/continue"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert [(m.role, m.content) for m in provider.received_messages or []] == [("user", "hi"), ("assistant", "orig")]
    await _refresh_after_finalize(db_session, conversation)
    live = next(
        m
        for m in await _messages(db_session, (await _turns(db_session, conversation))[0])
        if m.role is MessageRole.ASSISTANT and m.replaces_message_id is not None
    )
    assert unseal_content(live.content, encrypted=live.content_encrypted, settings=get_settings()) == "origmore"


async def test_continue_with_unfinalized_prior_carries_no_context(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Continuing a reply still streaming (never finalized) has no prior text — the prior is
    # neither fed as context nor prepended; the client gets only the fresh continuation.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="more"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)  # left streaming

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/continue"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert [e["event"] for e in _parse_sse(response.text)] == ["delta", "done"]
    assert [(m.role, m.content) for m in provider.received_messages or []] == [("user", "hi")]  # no prior assistant


async def test_regenerate_non_last_turn_returns_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # History is linear: once a newer turn exists, the earlier reply can no longer be
    # regenerated (it would feed the later turns back into the prompt).
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    first_assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)
    with as_user(user):  # a second turn makes the first reply no longer the latest
        await async_client_with_db.post(_url(conversation), json={"content": "again"})

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{first_assistant.id}/regenerate"), json=None)

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_continue_non_last_turn_returns_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # continue shares the supersede path, so the last-turn rule applies to it too.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    first_assistant = await _open_assistant(async_client_with_db, db_session, user, conversation)
    with as_user(user):  # a newer turn demotes the first reply
        await async_client_with_db.post(_url(conversation), json={"content": "again"})

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{first_assistant.id}/continue"), json=None)

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_regenerate_user_message_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    await _open_assistant(async_client_with_db, db_session, user, conversation)
    turn = (await _turns(db_session, conversation))[0]
    user_msg = next(m for m in await _messages(db_session, turn) if m.role is MessageRole.USER)

    with as_user(user):  # only an assistant reply can be regenerated
        response = await async_client_with_db.post(_url(conversation, f"/{user_msg.id}/regenerate"), json=None)

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_regenerate_unknown_message_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{uuid4()}/regenerate"), json=None)

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_masked_pre_stream_error_hides_model_identity(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model that cannot answer in text makes dispatch_stream raise ProviderBadRequestError on the
    # await (before any provider call); the message names the real model alias. Under a
    # masked evaluation the 400 detail must be the generic text, and the orphaned
    # placeholder must be finalized as `error` (not left `streaming`).
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=True, output_modalities=[Modality.IMAGE])
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == MASKED_ERROR_DETAIL  # real model alias not leaked

    turns = await _turns(db_session, conversation)
    assistant = next(m for m in await _messages(db_session, turns[0]) if m.role is MessageRole.ASSISTANT)
    assert assistant.status is MessageStatus.ERROR  # orphan finalized, not stuck streaming


async def test_unmasked_pre_stream_error_reveals_detail(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=False, output_modalities=[Modality.IMAGE])
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    detail = response.json()["detail"]
    assert detail != MASKED_ERROR_DETAIL  # unmasked evaluation surfaces the raw provider message
    assert "text output" in detail


async def test_replay_of_pre_stream_error_reconstructs_original_status(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-stream 400 (no text output) persists structured `extra`, so replaying the
    # same client_message_id reconstructs the 400 — not a generic 502.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=False, output_modalities=[Modality.IMAGE])
    _patch_provider(monkeypatch, _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")]))
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())

    with as_user(user):
        first = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
        replay = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    assert first.status_code == status.HTTP_400_BAD_REQUEST  # pre-stream capability rejection
    events = _parse_sse(replay.text)
    assert [e["event"] for e in events] == ["error"]
    assert json.loads(events[0]["data"])["status"] == 400  # original 400 preserved, not a generic 502


async def test_pre_stream_error_status_matches_on_first_call_and_replay(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ProviderContextWindowError maps to 400 but is NOT a ProviderBadRequestError — the
    # first call and the replay must agree (both 400), driven by the one status mapping.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=False)

    async def _raise_context_window(*_args: object, **_kwargs: object) -> object:
        raise ProviderContextWindowError("prompt exceeds the context window")

    monkeypatch.setattr("app.api.v1.messages.dispatch_stream", _raise_context_window)
    _keep_open(monkeypatch, db_session)
    cmid = str(uuid4())

    with as_user(user):
        first = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})
        replay = await async_client_with_db.post(_url(conversation), json={"content": "hi", "client_message_id": cmid})

    assert first.status_code == status.HTTP_400_BAD_REQUEST  # context-window is a client error, not 502
    events = _parse_sse(replay.text)
    assert [e["event"] for e in events] == ["error"]
    assert json.loads(events[0]["data"])["status"] == 400  # replay agrees with the first call


async def test_masked_pre_stream_provider_error_is_502_and_masked(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=True)
    _keep_open(monkeypatch, db_session)

    async def _raise(*_args: object, **_kwargs: object) -> object:
        raise ProviderAuthError("credential for model 'secret-alias' could not be decrypted")

    monkeypatch.setattr("app.api.v1.messages.dispatch_stream", _raise)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["detail"] == MASKED_ERROR_DETAIL  # provider text (names the model) is masked


async def _with_tags(db: AsyncSession, conversation: Conversation, tags: dict[str, str]) -> None:
    conversation.tags = tags
    db.add(conversation)
    await db.flush()


def _system_prompts(provider: _FakeProvider) -> list[str]:
    # The gateway pops `system_prompt` out of params and prepends it as a `system` message,
    # so the tag block lands here — not in `received_params`.
    return [str(m.content) for m in provider.received_messages or [] if m.role == "system"]


async def test_send_injects_tags_as_system_message(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod", "team": "red"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod\nteam: red"]


async def test_send_without_tags_prepends_no_system_message(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == []  # no tags, no base prompt → nothing injected


async def test_send_tags_appended_after_existing_system_prompt(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, conversation_params={"system_prompt": "You are helpful."})
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"You are helpful.\n\n{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_send_tags_survive_per_request_param_override(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ordering: tags are applied after the per-request override merge, so the override can't drop them.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "params": {"temperature": 0.2}}
        )

    assert response.status_code == status.HTTP_200_OK
    assert (provider.received_params or {}) == {"temperature": 0.2}  # system_prompt popped into the system message
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_send_suppresses_params_but_keeps_tags_for_a_flagged_model(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The end-to-end shape of the opt-out on the path real traffic takes: nothing from the
    # cascade or the per-request override reaches the provider, while the tag context still
    # does — otherwise the persisted reply would record context its prompt never carried.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(
        db_session,
        user,
        model_params={"temperature": 0.5, "system_prompt": "operator prompt"},
        assignment_params={"top_p": 0.9},
        conversation_params={"max_tokens": 128},
        advanced_params_disabled=True,
    )
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi", "params": {"seed": 7}})

    assert response.status_code == status.HTTP_200_OK
    assert (provider.received_params or {}) == {}
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_send_injects_tags_even_when_masked(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Masking hides model identity in projections/errors — it does NOT stop the operator's own tags
    # from reaching the model.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user, mask=True)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_second_send_still_injects_tags(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A subsequent turn flows through the same `_stream_into` seam as regenerate/continue, so tags
    # keep being injected on later exchanges too.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="a2"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "q1"})
    turn0 = (await _turns(db_session, conversation))[0]
    assistant0 = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant0.id, content="a1", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "q2"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_message_tags_injected_and_persisted(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi", "tags": {"turn": "1"}})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nturn: 1"]  # sent to the model
    turn0 = (await _turns(db_session, conversation))[0]
    user_msg = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.USER)
    assert user_msg.tags == {"turn": "1"}  # persisted on the user message


async def test_message_tags_override_conversation_tags(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod", "keep": "yes"})
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "tags": {"env": "dev", "turn": "1"}}
        )

    assert response.status_code == status.HTTP_200_OK
    # message tag `env` overrides the conversation's; `keep` (conv) and `turn` (msg) both included.
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: dev\nkeep: yes\nturn: 1"]


async def test_message_tags_do_not_leak_into_the_next_turn(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The one real failure mode of merging per-message over per-conversation tags: a turn's own tags
    # are request-scoped, so the next turn must fall back to the conversation's alone.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="a"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "one", "tags": {"turn": "1"}})
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod\nturn: 1"]
    turn0 = (await _turns(db_session, conversation))[0]
    assistant0 = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant0.id, content="a", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "two"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_message_tag_rejected_when_key_not_allowed(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    evaluation = (
        await db_session.execute(select(Evaluation).where(col(Evaluation.id) == conversation.evaluation_id))
    ).scalar_one()
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi", "tags": {"team": "red"}})

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def _evaluation_of(db: AsyncSession, conversation: Conversation) -> Evaluation:
    return (await db.execute(select(Evaluation).where(col(Evaluation.id) == conversation.evaluation_id))).scalar_one()


async def test_send_drops_conversation_tags_the_schema_stopped_allowing(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stored tags are replayed, not authored: tightening the schema stops them reaching the model,
    # but must not fail the turn — the owner cannot clear a tag the console no longer shows.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    conversation.tags = {"legacy": "value", "env": "prod"}
    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add_all([conversation, evaluation])
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()
    provider = _FakeProvider([_chunk(content="x"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod"]


async def test_send_still_rejects_a_disallowed_key_the_request_authors(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The other entry point: authoring is refused with the offending key named.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "tags": {"smuggled": "v"}}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "smuggled" in response.json()["detail"]


async def _completed_turn(
    client: AsyncClient, db: AsyncSession, user: User, conversation: Conversation, tags: dict[str, str]
) -> Message:
    """Send one tagged message and finalize its reply, so it can be regenerated / continued."""
    with as_user(user):
        await client.post(_url(conversation), json={"content": "hi", "tags": tags})
    turn0 = (await _turns(db, conversation))[0]
    assistant = next(m for m in await _messages(db, turn0) if m.role is MessageRole.ASSISTANT)
    # A no-op when the caller wired `_finalize_into` first: `persist_stream`'s own detached finalize
    # already completed the row, and this UPDATE's `WHERE status = 'streaming'` guard no longer matches.
    await finalize_message(db, assistant.id, content="first", status=MessageStatus.COMPLETE, encrypted=False)
    await db.commit()
    return assistant


@pytest.mark.parametrize("operation", ["regenerate", "continue"])
async def test_replaying_a_turn_drops_tags_the_schema_stopped_allowing(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    # Both siblings of send replay the superseded turn's tags into the prompt, so both have to apply
    # the policy — otherwise a key dropped from the allow-list keeps reaching the model through them.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="first"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    assistant = await _completed_turn(async_client_with_db, db_session, user, conversation, {"persona": "pirate"})

    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/{operation}"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == []


async def test_regenerate_reuses_the_turns_message_tags(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-answering the same prompt must use the same prompt context: the user message keeps its tags
    # and the transcript keeps showing them, so regenerating without them would invalidate the very
    # A/B comparison the feature exists for.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="first"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "tags": {"persona": "pirate"}})
    turn0 = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn0) if m.role is MessageRole.ASSISTANT)
    await finalize_message(db_session, assistant.id, content="first", status=MessageStatus.COMPLETE, encrypted=False)
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(_url(conversation, f"/{assistant.id}/regenerate"), json=None)

    assert response.status_code == status.HTTP_200_OK
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\npersona: pirate"]


async def test_idempotent_retry_survives_a_policy_tightened_after_the_turn_persisted(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The authored-tag gate judges what a request *writes*. A retry writes nothing — it replays a turn
    # the server already accepted — so gating it too made a persisted reply permanently unreachable
    # the moment an admin restricted the schema: the client could not tell "never persisted" from
    # "persisted but un-replayable", which is the whole point of the idempotency key.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    body = {"content": "hi", "client_message_id": str(uuid4()), "tags": {"persona": "pirate"}}
    with as_user(user):
        first = await async_client_with_db.post(_url(conversation), json=body)
    assert first.status_code == status.HTTP_200_OK

    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.commit()

    with as_user(user):
        retry = await async_client_with_db.post(_url(conversation), json=body)

    assert retry.status_code == status.HTTP_200_OK
    assert provider.calls == 1  # still a replay, not a regeneration
    assert len(await _turns(db_session, conversation)) == 1


async def test_authoring_a_disallowed_tag_is_still_rejected_and_never_reaches_the_provider(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of moving the gate after the replay resolution: a first send — which authors the
    # tag — must still 400, and must not generate. (Whether the turn flushed before the check survives
    # is not observable here: the request session is the test's own and `_keep_open` stops it closing,
    # while in production `get_db`'s `async with` discards the uncommitted work.)
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.commit()

    with as_user(user):
        response = await async_client_with_db.post(
            _url(conversation), json={"content": "hi", "tags": {"persona": "pirate"}}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "persona" in response.text
    assert provider.calls == 0


async def test_send_records_the_tag_context_it_sent(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Record and prompt asserted in one test: they are the same claim, and separating them would let
    # one drift green.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi", "tags": {"turn": "1"}})

    await _refresh_after_finalize(db_session, conversation)
    turn = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn) if m.role is MessageRole.ASSISTANT)
    assert assistant.status is MessageStatus.COMPLETE  # else a broken harness reads as a missing key
    assert assistant.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod", "turn": "1"}
    assert _system_prompts(provider) == [f"{TAG_CONTEXT_PREAMBLE}\nenv: prod\nturn: 1"]


@pytest.mark.parametrize("operation", ["regenerate", "continue"])
async def test_regenerate_and_continue_record_the_replayed_turns_tag_context(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    first_provider = _FakeProvider([_chunk(content="first"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, first_provider)
    _keep_open(monkeypatch, db_session)
    # Wired before the first turn too, so that turn's own finalize actually records a tag context —
    # otherwise "before" is just the placeholder default and the later equality proves nothing.
    _finalize_into(monkeypatch, db_session)
    assistant = await _completed_turn(async_client_with_db, db_session, user, conversation, {"persona": "pirate"})
    before = dict(assistant.extra)
    assert before == {"finish_reason": "stop", TAG_CONTEXT_EXTRA_KEY: {"persona": "pirate"}}  # pin: not vacuously {}

    provider = _FakeProvider([_chunk(content="again"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)

    with as_user(user):
        await async_client_with_db.post(_url(conversation, f"/{assistant.id}/{operation}"))

    await _refresh_after_finalize(db_session, conversation)
    await db_session.refresh(assistant)
    turn = (await _turns(db_session, conversation))[0]
    replacement = next(m for m in await _messages(db_session, turn) if m.replaces_message_id == assistant.id)
    superseded = await db_session.get(Message, assistant.id)
    assert replacement.status is MessageStatus.COMPLETE
    assert replacement.extra[TAG_CONTEXT_EXTRA_KEY] == {"persona": "pirate"}
    assert superseded is not None
    assert dict(superseded.extra) == before  # the old reply keeps its own record


async def test_send_without_tags_records_no_tag_context(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    await _refresh_after_finalize(db_session, conversation)
    turn = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn) if m.role is MessageRole.ASSISTANT)
    assert assistant.extra == {"finish_reason": "stop"}  # exact: no empty artefact


async def test_editing_conversation_tags_leaves_an_earlier_replys_record_untouched(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The point of the record: the transcript stops depending on what the tags say *now*.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})
    provider = _FakeProvider([_chunk(content="one"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "first"})
        await async_client_with_db.patch(
            f"/api/v1/evaluations/{conversation.evaluation_id}/conversations/{conversation.id}",
            json={"tags": {"env": "dev"}},
        )
        await async_client_with_db.post(_url(conversation), json={"content": "second"})

    await _refresh_after_finalize(db_session, conversation)
    replies = [
        m
        for turn in sorted(await _turns(db_session, conversation), key=lambda t: t.turn_index)
        for m in await _messages(db_session, turn)
        if m.role is MessageRole.ASSISTANT
    ]
    assert replies[0].extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}  # untouched by the edit
    assert replies[1].extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "dev"}  # and the next one moved with it


async def test_tags_the_policy_drops_are_not_recorded(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is post-policy by construction: every entry demonstrably reached the model.
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod", "legacy": "v1"})
    evaluation = await _evaluation_of(db_session, conversation)
    evaluation.tags_restricted = True
    db_session.add_all([evaluation, EvaluationTagKey(evaluation_id=evaluation.id, key="env")])
    await db_session.flush()
    provider = _FakeProvider([_chunk(content="hi"), _chunk(finish_reason="stop")])
    _patch_provider(monkeypatch, provider)
    _keep_open(monkeypatch, db_session)
    _finalize_into(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    await _refresh_after_finalize(db_session, conversation)
    turn = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn) if m.role is MessageRole.ASSISTANT)
    assert assistant.extra[TAG_CONTEXT_EXTRA_KEY] == {"env": "prod"}


async def test_a_pre_dispatch_failure_records_no_tag_context(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two of the three settle arms fail before the fold, so recording on one of them would make an
    # absent record ambiguous — "no tags" vs "not captured".
    user = await _user(db_session, ["conversations:update"])
    conversation = await _conversation(db_session, user)
    await _with_tags(db_session, conversation, {"env": "prod"})

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise ProviderRateLimitError("slow down")

    monkeypatch.setattr("app.api.v1.messages.dispatch_stream", _boom)
    _keep_open(monkeypatch, db_session)

    with as_user(user):
        await async_client_with_db.post(_url(conversation), json={"content": "hi"})

    db_session.expire_all()
    await db_session.refresh(conversation)
    turn = (await _turns(db_session, conversation))[0]
    assistant = next(m for m in await _messages(db_session, turn) if m.role is MessageRole.ASSISTANT)
    assert TAG_CONTEXT_EXTRA_KEY not in assistant.extra
    assert assistant.extra["error"]["status"] == status.HTTP_429_TOO_MANY_REQUESTS
