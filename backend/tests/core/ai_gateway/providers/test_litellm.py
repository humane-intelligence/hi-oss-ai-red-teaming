"""Unit tests for LiteLLMProvider — driven by litellm's `mock_response`, no network."""

from types import SimpleNamespace

import litellm
import pytest

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.chat import ImageUrl
from app.core.ai_gateway.chat import TextContentPart
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.providers.base import ModelProvider
from app.core.ai_gateway.providers.litellm import _LITELLM_PREFIX
from app.core.ai_gateway.providers.litellm import LiteLLMProvider
from app.core.ai_gateway.providers.litellm import _delta_role
from app.core.ai_gateway.providers.litellm import _map_error
from app.core.ai_gateway.providers.litellm import _safe_params
from app.core.ai_gateway.providers.litellm import _to_chunk
from app.core.ai_gateway.providers.litellm import _to_completion
from app.core.ai_gateway.providers.litellm import _to_usage


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    # litellm may fire usage telemetry over the network; --disable-socket would
    # turn that into a hard error, so switch it off for the mock-driven tests.
    monkeypatch.setattr(litellm, "telemetry", False)


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hi")]


@pytest.mark.unit
def test_litellm_provider_satisfies_the_port() -> None:
    assert isinstance(LiteLLMProvider(), ModelProvider)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        (ProviderVendor.OPENAI, "openai/m"),
        (ProviderVendor.ANTHROPIC, "anthropic/m"),
        (ProviderVendor.GOOGLE, "gemini/m"),
        (ProviderVendor.AZURE, "azure/m"),
        (ProviderVendor.AWS_BEDROCK, "bedrock/m"),
        (ProviderVendor.COHERE, "cohere/m"),
        (ProviderVendor.HUGGINGFACE, "huggingface/m"),
        (ProviderVendor.GENERIC, "openai/m"),
    ],
)
def test_model_string_built_per_vendor(vendor: ProviderVendor, expected: str) -> None:
    assert LiteLLMProvider._model(vendor, "m") == expected


@pytest.mark.unit
async def test_chat_returns_normalized_completion() -> None:
    completion = await LiteLLMProvider().chat(
        vendor=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        messages=_messages(),
        params={"mock_response": "Hello there"},
    )

    assert completion.choices[0].message.role == "assistant"
    assert completion.choices[0].message.content == "Hello there"
    assert "gpt-4o" in completion.model
    assert completion.usage is not None


@pytest.mark.unit
async def test_stream_yields_chunks_then_finish_reason() -> None:
    chunks = [
        chunk
        async for chunk in LiteLLMProvider().stream(
            vendor=ProviderVendor.OPENAI,
            provider_model_id="gpt-4o",
            messages=_messages(),
            params={"mock_response": "Hi there"},
        )
    ]

    content = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert content == "Hi there"
    # include_usage (OpenAI) appends a trailing usage chunk, so finish_reason
    # is no longer guaranteed on the last chunk — assert it appears at all.
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (litellm.RateLimitError(message="x", llm_provider="openai", model="m"), ProviderRateLimitError),
        (litellm.AuthenticationError(message="x", llm_provider="openai", model="m"), ProviderAuthError),
        (litellm.Timeout(message="x", model="m", llm_provider="openai"), ProviderTimeoutError),
        (litellm.BadRequestError(message="x", model="m", llm_provider="openai"), ProviderBadRequestError),
        (litellm.ContextWindowExceededError(message="x", model="m", llm_provider="openai"), ProviderContextWindowError),
        (litellm.ServiceUnavailableError(message="x", llm_provider="openai", model="m"), ProviderUnavailableError),
        (litellm.APIConnectionError(message="x", llm_provider="openai", model="m"), ProviderUnavailableError),
        (litellm.InternalServerError(message="x", llm_provider="openai", model="m"), ProviderUnavailableError),
    ],
)
def test_map_error_translates_litellm_taxonomy(exc: Exception, expected: type[ProviderError]) -> None:
    assert type(_map_error(exc)) is expected


@pytest.mark.unit
def test_map_error_unknown_falls_back_to_base() -> None:
    assert type(_map_error(ValueError("boom"))) is ProviderError


@pytest.mark.unit
def test_safe_params_drops_reserved_keeps_rest() -> None:
    cleaned = _safe_params({"model": "x", "messages": [], "stream": True, "temperature": 0.3})

    assert cleaned == {"temperature": 0.3}


@pytest.mark.unit
async def test_chat_ignores_reserved_params_instead_of_raising() -> None:
    # A stray reserved key in params would collide with the adapter's own
    # kwargs and raise TypeError; it must be dropped, not forwarded.
    completion = await LiteLLMProvider().chat(
        vendor=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        messages=_messages(),
        params={"model": "evil", "mock_response": "ok"},
    )

    assert completion.choices[0].message.content == "ok"


@pytest.mark.unit
async def test_chat_maps_malformed_response_to_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bad_response(**_kwargs: object) -> object:
        return object()  # no .id/.choices — normalization will fail

    monkeypatch.setattr(litellm, "acompletion", _bad_response)

    with pytest.raises(ProviderError):
        await LiteLLMProvider().chat(
            vendor=ProviderVendor.OPENAI,
            provider_model_id="gpt-4o",
            messages=_messages(),
        )


@pytest.mark.unit
def test_litellm_prefix_covers_every_vendor() -> None:
    # A vendor added to the enum without a prefix would KeyError at dispatch.
    assert set(_LITELLM_PREFIX) == set(ProviderVendor)


@pytest.mark.unit
def test_global_litellm_config_is_set() -> None:
    assert litellm.drop_params is True
    assert litellm.telemetry is False
    assert litellm.num_retries == 0


@pytest.mark.unit
async def test_stream_maps_midstream_error_to_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stream that yields, then the upstream drops mid-flight: the partial
    # chunk must reach the caller and the failure must arrive as a ProviderError.
    async def _flaky(**_kwargs: object) -> object:
        async def _gen() -> object:
            yield SimpleNamespace(
                id="c",
                model="gpt-4o",
                created=1,
                choices=[
                    SimpleNamespace(
                        index=0, finish_reason=None, delta=SimpleNamespace(role="assistant", content="part")
                    )
                ],
            )
            raise litellm.APIConnectionError(message="drop", llm_provider="openai", model="m")

        return _gen()

    monkeypatch.setattr(litellm, "acompletion", _flaky)

    received: list[ChatChunk] = []

    async def _drain() -> None:
        async for chunk in LiteLLMProvider().stream(
            vendor=ProviderVendor.OPENAI,
            provider_model_id="gpt-4o",
            messages=_messages(),
        ):
            received.append(chunk)  # noqa: PERF401 — must keep partial chunks when the stream raises mid-way

    with pytest.raises(ProviderUnavailableError):
        await _drain()

    assert len(received) == 1
    assert received[0].choices[0].delta.content == "part"


def _one_chunk_stream(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch acompletion to capture kwargs and return a one-chunk async stream."""
    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> object:
        captured.update(kwargs)

        async def _gen() -> object:
            yield SimpleNamespace(
                id="c",
                model="gpt-4o",
                created=1,
                choices=[
                    SimpleNamespace(index=0, finish_reason="stop", delta=SimpleNamespace(role=None, content="hi"))
                ],
            )

        return _gen()

    monkeypatch.setattr(litellm, "acompletion", _capture)
    return captured


@pytest.mark.unit
async def test_stream_requests_usage_for_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _one_chunk_stream(monkeypatch)

    async for _ in LiteLLMProvider().stream(vendor=ProviderVendor.GENERIC, provider_model_id="m", messages=_messages()):
        pass

    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}


@pytest.mark.unit
async def test_stream_omits_usage_option_for_non_openai_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _one_chunk_stream(monkeypatch)

    async for _ in LiteLLMProvider().stream(
        vendor=ProviderVendor.ANTHROPIC, provider_model_id="m", messages=_messages()
    ):
        pass

    assert "stream_options" not in captured


@pytest.mark.unit
def test_to_chunk_carries_usage_from_empty_choices_chunk() -> None:
    # The include_usage terminal chunk has no choices and only usage.
    chunk = SimpleNamespace(
        id="c",
        model="gpt-4o",
        created=1,
        choices=[],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    )

    result = _to_chunk(chunk)

    assert result.choices == []
    assert result.usage is not None
    assert result.usage.total_tokens == 8


@pytest.mark.unit
def test_to_usage_tolerates_partial_fields() -> None:
    usage = _to_usage(SimpleNamespace(total_tokens=30))

    assert usage.total_tokens == 30
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None


@pytest.mark.unit
def test_to_completion_pins_assistant_role() -> None:
    # A provider returning an unexpected role must not fail normalization.
    response = SimpleNamespace(
        id="x",
        model="gpt-4o",
        created=1,
        choices=[SimpleNamespace(index=0, finish_reason="stop", message=SimpleNamespace(role="tool", content="hi"))],
        usage=None,
    )

    completion = _to_completion(response)

    assert completion.choices[0].message.role == "assistant"
    assert completion.usage is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("assistant", "assistant"),
        ("system", "system"),
        ("user", "user"),
        ("tool", None),
        ("developer", None),
        (None, None),
    ],
)
def test_delta_role_drops_unknown_roles(role: str | None, expected: str | None) -> None:
    assert _delta_role(role) == expected


@pytest.mark.unit
async def test_chat_forwards_generation_params_to_acompletion(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            id="x",
            model="gpt-4o",
            created=1,
            choices=[
                SimpleNamespace(index=0, finish_reason="stop", message=SimpleNamespace(role="assistant", content="ok"))
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", _capture)

    await LiteLLMProvider().chat(
        vendor=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        messages=_messages(),
        api_key="sk-test",
        api_base="https://proxy/v1",
        params={"temperature": 0.3},
    )

    assert captured["model"] == "openai/gpt-4o"
    assert captured["temperature"] == 0.3
    assert captured["api_key"] == "sk-test"
    assert captured["api_base"] == "https://proxy/v1"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.unit
async def test_chat_forwards_multimodal_content_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    # A multi-modal message serialises straight into litellm's OpenAI-shape content
    # list (text + image_url) via model_dump — the adapter forwards it unchanged.
    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            id="x",
            model="gpt-4o",
            created=1,
            choices=[
                SimpleNamespace(index=0, finish_reason="stop", message=SimpleNamespace(role="assistant", content="ok"))
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", _capture)

    message = ChatMessage(
        role="user",
        content=[
            TextContentPart(text="what is this?"),
            ImageContentPart(image_url=ImageUrl(url="data:image/png;base64,AAAA")),
        ],
    )
    await LiteLLMProvider().chat(vendor=ProviderVendor.OPENAI, provider_model_id="gpt-4o", messages=[message])

    assert captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
