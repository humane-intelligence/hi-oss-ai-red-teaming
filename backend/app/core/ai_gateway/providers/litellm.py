"""LiteLLM-backed implementation of the `ModelProvider` port.

The only module that imports `litellm`. It maps our value types onto
`litellm.acompletion` and litellm's exception taxonomy onto `ProviderError`,
so the rest of the app never touches the provider library directly. Swapping
LiteLLM for another backend means a new adapter here — nothing else moves.
"""

from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import Role
from app.core.ai_gateway.chat import Usage
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

# drop_params: drop a param a provider rejects, so a row can carry provider-specific
# knobs (top_k) without breaking other providers. num_retries=0: retry is the
# consumer's policy. telemetry off: no outbound pings (also keeps the sandbox happy).
litellm.drop_params = True
litellm.telemetry = False  # ty: ignore[invalid-assignment]
litellm.num_retries = 0

_DEFAULT_TIMEOUT = 60.0

# vendor -> litellm "<prefix>/<model>" routing prefix. `generic` = OpenAI-compatible
# endpoint, so it rides the openai prefix with a caller-supplied api_base.
_LITELLM_PREFIX: dict[ProviderVendor, str] = {
    ProviderVendor.OPENAI: "openai",
    ProviderVendor.ANTHROPIC: "anthropic",
    ProviderVendor.GOOGLE: "gemini",
    ProviderVendor.AZURE: "azure",
    ProviderVendor.AWS_BEDROCK: "bedrock",
    ProviderVendor.COHERE: "cohere",
    ProviderVendor.HUGGINGFACE: "huggingface",
    ProviderVendor.GENERIC: "openai",
}

# Kwargs the adapter sets itself; if a row's params carry them too, acompletion
# gets duplicate keyword args. Dropped (with a warning) rather than passed.
_RESERVED_PARAMS = frozenset({"model", "messages", "api_key", "api_base", "timeout", "stream"})

# OpenAI-compatible vendors omit usage from a stream unless asked (the final
# chunk then carries usage with an empty choices list). Anthropic streams usage
# natively, others vary — so the flag is scoped to the OpenAI prefix only.
_USAGE_IN_STREAM_VENDORS = frozenset({ProviderVendor.OPENAI, ProviderVendor.GENERIC})


def _safe_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    reserved = _RESERVED_PARAMS & params.keys()
    if reserved:
        logger.warning("ai.params.reserved_dropped", keys=sorted(reserved))
        return {k: v for k, v in params.items() if k not in _RESERVED_PARAMS}
    return params


class LiteLLMProvider:
    """`ModelProvider` adapter delegating to `litellm.acompletion`."""

    def __init__(self, default_timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._default_timeout = default_timeout

    async def chat(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — forwarded to litellm's own (httpx) timeout
    ) -> ChatCompletion:
        model = self._model(vendor, provider_model_id)
        logger.info("ai.chat.start", model=model, message_count=len(messages), stream=False)
        try:
            response = await litellm.acompletion(
                **self._call_kwargs(model, messages, api_key, api_base, params, timeout)
            )
            completion = _to_completion(response)
        except Exception as exc:
            logger.warning("ai.chat.error", model=model, error_class=type(exc).__name__)
            raise _map_error(exc) from exc
        logger.info(
            "ai.chat.ok",
            model=model,
            usage=completion.usage.model_dump() if completion.usage else None,
        )
        return completion

    async def stream(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — forwarded to litellm's own (httpx) timeout
    ) -> AsyncIterator[ChatChunk]:
        model = self._model(vendor, provider_model_id)
        logger.info("ai.chat.start", model=model, message_count=len(messages), stream=True)
        call_kwargs = self._call_kwargs(model, messages, api_key, api_base, params, timeout)
        if vendor in _USAGE_IN_STREAM_VENDORS:
            # setdefault, not assign: a row whose stored params already pin
            # stream_options keeps its value (it isn't a reserved kwarg).
            call_kwargs.setdefault("stream_options", {"include_usage": True})
        try:
            stream = await litellm.acompletion(stream=True, **call_kwargs)
            async for chunk in stream:
                yield _to_chunk(chunk)
            logger.info("ai.chat.ok", model=model, stream=True)
        except Exception as exc:
            logger.warning("ai.chat.error", model=model, error_class=type(exc).__name__)
            raise _map_error(exc) from exc

    def _call_kwargs(
        self,
        model: str,
        messages: list[ChatMessage],
        api_key: str | None,
        api_base: str | None,
        params: dict[str, Any] | None,
        timeout: float | None,
    ) -> dict[str, Any]:
        # Single source for the acompletion kwargs shared by chat and stream —
        # only `stream` differs, so changing a shared knob touches one place.
        return {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "api_key": api_key,
            "api_base": api_base,
            "timeout": timeout if timeout is not None else self._default_timeout,
            **_safe_params(params),
        }

    @staticmethod
    def _model(vendor: ProviderVendor, provider_model_id: str) -> str:
        prefix = _LITELLM_PREFIX.get(vendor)
        if prefix is None:
            # Unmapped vendor: a ProviderError, not a raw KeyError 500.
            raise ProviderBadRequestError(f"No litellm routing for provider vendor {vendor!r}.")
        return f"{prefix}/{provider_model_id}"


# Ordered most-specific-first: ContextWindowExceededError subclasses
# BadRequestError, so it must precede it. Matched top-down; first hit wins.
_ERROR_MAP: tuple[tuple[type[Exception], type[ProviderError]], ...] = (
    (litellm.ContextWindowExceededError, ProviderContextWindowError),
    (litellm.AuthenticationError, ProviderAuthError),
    (litellm.RateLimitError, ProviderRateLimitError),
    (litellm.Timeout, ProviderTimeoutError),
    (litellm.BadRequestError, ProviderBadRequestError),
    (litellm.ServiceUnavailableError, ProviderUnavailableError),
    (litellm.APIConnectionError, ProviderUnavailableError),
    (litellm.InternalServerError, ProviderUnavailableError),
)


def _map_error(exc: Exception) -> ProviderError:
    """Translate a litellm/openai exception into the gateway's taxonomy.

    Anything unrecognised degrades to the `ProviderError` base rather than
    leaking the library's exception type past the adapter boundary.
    """
    for exc_type, provider_error in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return provider_error(str(exc))
    return ProviderError(str(exc))


_VALID_ROLES = frozenset({"system", "user", "assistant"})


def _delta_role(role: Any) -> Role | None:
    # Drop roles we don't model (e.g. "tool") to None rather than fail validation.
    return role if role in _VALID_ROLES else None


def _to_usage(usage: Any) -> Usage:
    # Fields optional: a provider reporting only a subset must not crash normalization.
    return Usage(
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def _to_completion(response: Any) -> ChatCompletion:
    return ChatCompletion(
        id=response.id,
        model=response.model,
        created=response.created,
        choices=[
            ChatChoice(
                index=choice.index,
                # A completion choice is always the assistant's reply; pin it.
                message=ChatMessage(role="assistant", content=choice.message.content or ""),
                finish_reason=choice.finish_reason,
            )
            for choice in response.choices
        ],
        usage=_to_usage(response.usage) if getattr(response, "usage", None) else None,
    )


def _to_chunk(chunk: Any) -> ChatChunk:
    return ChatChunk(
        id=chunk.id,
        model=getattr(chunk, "model", "") or "",
        created=getattr(chunk, "created", 0) or 0,
        choices=[
            ChunkChoice(
                index=choice.index,
                delta=ChatMessageDelta(role=_delta_role(choice.delta.role), content=choice.delta.content),
                finish_reason=choice.finish_reason,
            )
            for choice in chunk.choices
        ],
        usage=_to_usage(chunk.usage) if getattr(chunk, "usage", None) else None,
    )
