"""Integration tests for dispatch_chat / dispatch_stream — routing over a real registry row.

A recording fake stands in for the provider so these exercise the dispatch
logic (lookup, credential resolution, param merge) without touching litellm.
"""

import asyncio
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.chat import ImageUrl
from app.core.ai_gateway.chat import TextContentPart
from app.core.ai_gateway.crypto import SecretDecryptError
from app.core.ai_gateway.dispatch import _outcome
from app.core.ai_gateway.dispatch import dispatch_chat
from app.core.ai_gateway.dispatch import dispatch_stream
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.services.ai_models import create_model
from app.core.config import Settings
from app.core.exceptions import NotFoundError


def _session_provider(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """A stamp-session provider yielding the test session — keeps `_stamp` off the
    uninitialised engine, whose swallowed RuntimeError spams ERROR logs per call."""

    @asynccontextmanager
    async def provider() -> AsyncIterator[AsyncSession]:
        yield session

    return provider


class _RecordingProvider:
    """Captures the kwargs dispatch passes through and returns a canned reply."""

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}
        self.stream_closed = False

    async def chat(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors the ModelProvider port signature
    ) -> ChatCompletion:
        self.last = {
            "vendor": vendor,
            "provider_model_id": provider_model_id,
            "messages": messages,
            "api_key": api_key,
            "api_base": api_base,
            "params": params,
            "timeout": timeout,
        }
        return ChatCompletion(
            id="x",
            model=provider_model_id,
            created=1,
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="ok"))],
        )

    async def stream(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors the ModelProvider port signature
    ) -> AsyncIterator[ChatChunk]:
        self.last = {"vendor": vendor, "provider_model_id": provider_model_id}
        try:
            yield ChatChunk(
                id="x",
                model=provider_model_id,
                created=1,
                choices=[ChunkChoice(index=0, delta=ChatMessageDelta(content="ok"), finish_reason="stop")],
            )
        finally:
            self.stream_closed = True


def _user_msg() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hi")]


def _image_msg() -> list[ChatMessage]:
    return [
        ChatMessage(
            role="user",
            content=[
                TextContentPart(text="what is this?"),
                ImageContentPart(image_url=ImageUrl(url="data:image/png;base64,AAAA")),
            ],
        )
    ]


@pytest.mark.integration
async def test_dispatch_routes_row_attributes_to_provider(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="GPT-4o",
        model_alias="gpt-4o-alias",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        inference_endpoint="https://proxy.internal/v1",
        parameters={"temperature": 0.5},
    )
    fake = _RecordingProvider()

    completion = await dispatch_chat(
        db_session,
        app_settings,
        model_alias="gpt-4o-alias",
        messages=_user_msg(),
        params={"top_p": 0.9},
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert completion.choices[0].message.content == "ok"
    assert fake.last["vendor"] is ProviderVendor.OPENAI
    assert fake.last["provider_model_id"] == "gpt-4o"
    assert fake.last["api_base"] == "https://proxy.internal/v1"
    assert fake.last["params"] == {"temperature": 0.5, "top_p": 0.9}


@pytest.mark.integration
async def test_dispatch_prepends_system_prompt_and_strips_it_from_params(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Sysprompt",
        model_alias="sysprompt",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"system_prompt": "You are a red-team target.", "temperature": 0.5},
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="sysprompt",
        messages=_user_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    sent: list[ChatMessage] = fake.last["messages"]
    assert sent[0].role == "system"
    assert sent[0].content == "You are a red-team target."
    assert sent[1].role == "user"
    # system_prompt is a gateway concern, not a litellm kwarg — must not leak into params.
    assert "system_prompt" not in fake.last["params"]
    assert fake.last["params"] == {"temperature": 0.5}


@pytest.mark.integration
async def test_dispatch_suppresses_every_param_when_advanced_params_disabled(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    """The flag drops the row's own knobs *and* whatever the caller merged on top."""
    await create_model(
        db_session,
        app_settings,
        name="Suppressed",
        model_alias="suppressed",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"system_prompt": "You are a red-team target.", "temperature": 0.5},
        advanced_params_disabled=True,
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="suppressed",
        messages=_user_msg(),
        params={"top_p": 0.3},
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["params"] == {}
    # The row's system_prompt is an operator knob too — no system message survives.
    assert [m.role for m in fake.last["messages"]] == ["user"]


@pytest.mark.integration
async def test_dispatch_keeps_system_suffix_when_advanced_params_disabled(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    """Tag context rides its own channel, so suppression must not silence it.

    Otherwise a reply would record tag context its prompt never carried.
    """
    await create_model(
        db_session,
        app_settings,
        name="Suffix suppressed",
        model_alias="suffix-suppressed",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"system_prompt": "operator prompt", "temperature": 0.5},
        advanced_params_disabled=True,
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="suffix-suppressed",
        messages=_user_msg(),
        provider=fake,
        system_suffix="env: prod",
        session_provider=_session_provider(db_session),
    )

    sent: list[ChatMessage] = fake.last["messages"]
    assert sent[0].role == "system"
    # The suppressed operator prompt is gone; only the gateway-owned suffix remains.
    assert sent[0].content == "env: prod"
    assert fake.last["params"] == {}


@pytest.mark.integration
async def test_dispatch_appends_system_suffix_after_operator_prompt(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Suffix append",
        model_alias="suffix-append",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"system_prompt": "You are helpful."},
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="suffix-append",
        messages=_user_msg(),
        provider=fake,
        system_suffix="env: prod",
        session_provider=_session_provider(db_session),
    )

    assert fake.last["messages"][0].content == "You are helpful.\n\nenv: prod"


@pytest.mark.integration
async def test_dispatch_translates_stop_sequences_to_stop(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Stopper",
        model_alias="stopper",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"stop_sequences": ["END"]},
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="stopper",
        messages=_user_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    # litellm's kwarg is `stop`; the registry's `stop_sequences` must be translated, not dropped.
    assert fake.last["params"] == {"stop": ["END"]}


@pytest.mark.integration
async def test_dispatch_rejects_model_without_text_output(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Imager",
        model_alias="imager",
        provider=ProviderVendor.OPENAI,
        provider_model_id="dall-e-3",
        output_modalities=[Modality.IMAGE],
    )
    # Expire so the gate reads the row back from Postgres as `list[str]`, not the
    # `list[Modality]` still in the identity map. The whole "plain text[], not a DB
    # enum" decision rests on StrEnum membership holding across that boundary.
    db_session.expire_all()

    with pytest.raises(ProviderBadRequestError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="imager",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_allows_image_content_for_vision_model(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Vision",
        model_alias="vision",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    db_session.expire_all()
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="vision",
        messages=_image_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert isinstance(fake.last["messages"][0].content, list)


@pytest.mark.integration
async def test_dispatch_allows_model_producing_text_and_image(db_session: AsyncSession, app_settings: Settings) -> None:
    # Output is a set, not the old either/or enum: a model that also emits images
    # still answers in text, so the gate must let it through.
    await create_model(
        db_session,
        app_settings,
        name="Multi",
        model_alias="multi-out",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        output_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    # Forces a reload, so the gate sees the `str`s the ARRAY(String) column returns
    # rather than the `Modality` members still sitting in the identity map.
    db_session.expire_all()
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="multi-out",
        messages=_user_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["provider_model_id"] == "gpt-4o"


@pytest.mark.integration
async def test_dispatch_rejects_image_content_for_non_vision_model(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # A text-only model (input_modalities defaults to text alone) must refuse a message
    # carrying an image part — a clean bad-request, not a leak to the provider.
    await create_model(
        db_session,
        app_settings,
        name="TextOnly",
        model_alias="text-only",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )

    with pytest.raises(ProviderBadRequestError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="text-only",
            messages=_image_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_rejects_generic_model_without_inference_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # A row predating the write-side rule — the service refuses to make one, so null the
    # URL after the fact. Left dispatchable it would ride litellm's OpenAI defaults on the
    # platform key instead of the self-hosted endpoint it claims to be.
    model = await create_model(
        db_session,
        app_settings,
        name="Legacy generic",
        model_alias="legacy-generic",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://slm:8080/v1",
    )
    model.inference_endpoint = None
    db_session.add(model)
    await db_session.flush()

    with pytest.raises(ProviderBadRequestError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="legacy-generic",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_call_params_override_row_params(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Override",
        model_alias="override",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        parameters={"temperature": 0.5},
    )
    fake = _RecordingProvider()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="override",
        messages=_user_msg(),
        params={"temperature": 0.9},
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["params"]["temperature"] == 0.9


@pytest.mark.integration
async def test_dispatch_unknown_alias_raises_not_found(db_session: AsyncSession, app_settings: Settings) -> None:
    with pytest.raises(NotFoundError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="does-not-exist",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_disabled_model_raises_not_found(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Disabled",
        model_alias="disabled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        is_disabled=True,
    )

    with pytest.raises(NotFoundError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="disabled",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_decrypt_failure_maps_to_auth_error(
    db_session: AsyncSession,
    app_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Rotated",
        model_alias="rotated",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise SecretDecryptError("key rotated")

    monkeypatch.setattr("app.core.ai_gateway.dispatch.resolve_api_key", _raise)

    with pytest.raises(ProviderAuthError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="rotated",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


@pytest.mark.integration
async def test_dispatch_stream_yields_chunks(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Streamer",
        model_alias="streamer",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    fake = _RecordingProvider()

    stream = await dispatch_stream(
        db_session,
        app_settings,
        model_alias="streamer",
        messages=_user_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )
    chunks = [chunk async for chunk in stream]

    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.content == "ok"
    assert chunks[0].choices[0].finish_reason == "stop"


@pytest.mark.integration
async def test_dispatch_stream_resolution_error_surfaces_on_await(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Eager (not an async generator): the lookup error must fire when awaited,
    # before any iteration, so an SSE caller hasn't committed a 200 yet.
    with pytest.raises(NotFoundError):
        await dispatch_stream(
            db_session,
            app_settings,
            model_alias="nope",
            messages=_user_msg(),
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


class _FailingProvider:
    """Raises the given ProviderError from chat and stream."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        raise self._exc

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise self._exc
        yield  # unreachable — makes this an async generator like the port expects


class _CloseFailingProvider:
    """Streams one chunk, then raises while its generator is being closed."""

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        raise NotImplementedError

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        try:
            yield ChatChunk(
                id="x",
                model="gpt-4o",
                created=1,
                choices=[ChunkChoice(index=0, delta=ChatMessageDelta(content="ok"), finish_reason="stop")],
            )
        finally:
            raise RuntimeError("provider cleanup failed")


def _calls_total(outcome: str) -> float:
    return REGISTRY.get_sample_value("redteam_model_calls_total", {"provider": "openai", "outcome": outcome}) or 0.0


def _durations_count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "redteam_model_call_duration_seconds_count", {"provider": "openai", "outcome": outcome}
        )
        or 0.0
    )


@pytest.mark.integration
async def test_dispatch_chat_records_ok_call_metrics(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Metered",
        model_alias="metered",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    calls_before = _calls_total("ok")
    durations_before = _durations_count("ok")

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="metered",
        messages=_user_msg(),
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )

    assert _calls_total("ok") == calls_before + 1
    assert _durations_count("ok") == durations_before + 1


@pytest.mark.integration
async def test_dispatch_chat_records_provider_failure_outcome(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Throttled",
        model_alias="throttled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    before = _calls_total("rate_limit")

    with pytest.raises(ProviderRateLimitError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="throttled",
            messages=_user_msg(),
            provider=_FailingProvider(ProviderRateLimitError("429")),
            session_provider=_session_provider(db_session),
        )

    assert _calls_total("rate_limit") == before + 1


@pytest.mark.integration
async def test_dispatch_stream_records_metric_on_exhaustion(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="MeteredStream",
        model_alias="metered-stream",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    before = _calls_total("ok")

    stream = await dispatch_stream(
        db_session,
        app_settings,
        model_alias="metered-stream",
        messages=_user_msg(),
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )
    _ = [chunk async for chunk in stream]

    assert _calls_total("ok") == before + 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderRateLimitError("x"), "rate_limit"),
        (ProviderTimeoutError("x"), "timeout"),
        (ProviderAuthError("x"), "auth"),
        (ProviderContextWindowError("x"), "context_window"),
        (ProviderBadRequestError("x"), "bad_request"),
        (ProviderUnavailableError("x"), "unavailable"),
        (ProviderError("x"), "error"),
        (GeneratorExit(), "aborted"),
        (asyncio.CancelledError(), "aborted"),
        (ValueError("x"), "error"),
    ],
)
def test_outcome_maps_exception_taxonomy(exc: BaseException, expected: str) -> None:
    assert _outcome(exc) == expected


@pytest.mark.integration
async def test_dispatch_stream_records_mid_stream_failure_outcome(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    await create_model(
        db_session,
        app_settings,
        name="StreamThrottled",
        model_alias="stream-throttled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    before = _calls_total("rate_limit")

    stream = await dispatch_stream(
        db_session,
        app_settings,
        model_alias="stream-throttled",
        messages=_user_msg(),
        provider=_FailingProvider(ProviderRateLimitError("429")),
        session_provider=_session_provider(db_session),
    )
    with pytest.raises(ProviderRateLimitError):
        _ = [chunk async for chunk in stream]

    assert _calls_total("rate_limit") == before + 1


@pytest.mark.integration
async def test_dispatch_stream_records_aborted_on_consumer_close(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    await create_model(
        db_session,
        app_settings,
        name="Abandoned",
        model_alias="abandoned",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    before = _calls_total("aborted")
    fake = _RecordingProvider()

    stream = await dispatch_stream(
        db_session,
        app_settings,
        model_alias="abandoned",
        messages=_user_msg(),
        provider=fake,
        session_provider=_session_provider(db_session),
    )
    assert isinstance(stream, AsyncGenerator)
    await anext(stream)
    await stream.aclose()

    assert _calls_total("aborted") == before + 1
    # The wrapper must close the provider generator deterministically, not leave it to GC.
    assert fake.stream_closed


@pytest.mark.integration
async def test_dispatch_stream_swallows_provider_close_error(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="CloseBoom",
        model_alias="close-boom",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    before = _calls_total("aborted")

    stream = await dispatch_stream(
        db_session,
        app_settings,
        model_alias="close-boom",
        messages=_user_msg(),
        provider=_CloseFailingProvider(),
        session_provider=_session_provider(db_session),
    )
    assert isinstance(stream, AsyncGenerator)
    await anext(stream)
    # A provider raising during cleanup must not surface out of the wrapper's close.
    await stream.aclose()

    assert _calls_total("aborted") == before + 1
