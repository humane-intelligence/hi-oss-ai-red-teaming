"""Gateway entry point: turn a registry alias into a provider call.

`dispatch_chat` / `dispatch_stream` are what domain code (conversations,
evaluations) calls — it names a model by its `AiModel.model_alias` and gets a
normalized `ChatCompletion` / `ChatChunk` stream back. Provider selection,
credential resolution, and the LiteLLM details all stay hidden behind here.

Imported directly (`from app.core.ai_gateway.dispatch import dispatch_chat`)
rather than re-exported from the package, so importing the rest of
`ai_gateway` (CRUD, models) does not pull litellm in.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from prometheus_client import Counter
from prometheus_client import Histogram
from sqlalchemy import or_
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.chat import ImageUrl
from app.core.ai_gateway.chat import TextContentPart
from app.core.ai_gateway.crypto import SecretDecryptError
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import WarmupStatus
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.inference_params import merge_inference_params
from app.core.ai_gateway.models import MISMATCH_MAX_LEN
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.providers.base import ModelProvider
from app.core.ai_gateway.providers.litellm import LiteLLMProvider
from app.core.ai_gateway.services.ai_models import missing_inference_endpoint
from app.core.ai_gateway.services.ai_models import resolve_api_key
from app.core.config import Settings
from app.core.database import SessionProvider
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_PROVIDER: ModelProvider = LiteLLMProvider()

_PROBE_TIMEOUT = 10.0
# Coarsest resolution the inactivity alert needs from `last_warmup_at`; anything finer is
# write amplification, since clients poll a waking endpoint every few seconds.
_WARMUP_STAMP_INTERVAL = timedelta(minutes=5)
_PROBE_MESSAGE = ChatMessage(role="user", content="ping")
# 1x1 transparent PNG — the smallest thing that is unambiguously an image, so the
# probe tests whether the endpoint takes image parts at all, not whether it can
# describe a particular picture.
_PROBE_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_PROBE_IMAGE_MESSAGE = ChatMessage(
    role="user",
    content=[TextContentPart(text="ping"), ImageContentPart(image_url=ImageUrl(url=_PROBE_PNG))],
)

MODEL_CALLS = Counter(
    "redteam_model_calls_total",
    "Provider calls dispatched through the AI gateway, by outcome.",
    ("provider", "outcome"),
)
MODEL_CALL_DURATION = Histogram(
    "redteam_model_call_duration_seconds",
    "Wall-clock duration of provider calls (streams: first iteration to exhaustion).",
    ("provider", "outcome"),
    # LLM calls routinely exceed prometheus' default 10s bucket ceiling.
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

_ERROR_OUTCOMES: tuple[tuple[type[ProviderError], str], ...] = (
    (ProviderRateLimitError, "rate_limit"),
    (ProviderTimeoutError, "timeout"),
    (ProviderAuthError, "auth"),
    (ProviderContextWindowError, "context_window"),
    (ProviderBadRequestError, "bad_request"),
    (ProviderUnavailableError, "unavailable"),
)


def _outcome(exc: BaseException) -> str:
    for exc_type, label in _ERROR_OUTCOMES:
        if isinstance(exc, exc_type):
            return label
    # Consumer walked away (client disconnect / task cancellation) — not a provider fault.
    if isinstance(exc, GeneratorExit | asyncio.CancelledError):
        return "aborted"
    return "error"


def _record(provider: str, outcome: str, started: float) -> None:
    MODEL_CALLS.labels(provider=provider, outcome=outcome).inc()
    MODEL_CALL_DURATION.labels(provider=provider, outcome=outcome).observe(time.perf_counter() - started)


async def _observed_stream(stream: AsyncIterator[ChatChunk], provider: str) -> AsyncIterator[ChatChunk]:
    started = time.perf_counter()
    outcome = "ok"
    try:
        async for chunk in stream:
            yield chunk
    except BaseException as exc:
        outcome = _outcome(exc)
        raise
    finally:
        _record(provider, outcome, started)
        # Close the wrapped provider generator deterministically — before this
        # wrapper existed the consumer closed it directly; don't leave it to GC.
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            # A provider raising during close must not mask the in-flight GeneratorExit.
            with contextlib.suppress(Exception):
                await aclose()


async def _stamp(
    model_id: UUID,
    values: dict[str, Any],
    session_provider: SessionProvider,
    *extra_where: ColumnElement[bool],
) -> None:
    """Write usage bookkeeping for `model_id` on its own committed session — best-effort.

    Not the caller's session: `get_db` never commits (only `@transactional` does) and both
    streaming call sites close it uncommitted right after dispatch returns, so a stamp there
    would roll back unwritten. Never raises — inactivity bookkeeping must not fail the call
    it observes, and a lost stamp costs at most one spurious alert. Both arms mirror
    `conversations.services.generation._finalize`, differing only in volume.
    """
    try:
        async with session_provider() as session:
            # Pin `updated_at`: naming it in the SET clause suppresses the column's `onupdate`,
            # which would otherwise make a public "last update" timestamp move on every message
            # and every warmup poll. These are system writes, not admin edits.
            statement = (
                update(AiModel)
                .where(col(AiModel.id) == model_id, *extra_where)
                .values(**values, updated_at=col(AiModel.updated_at))
            )
            await session.execute(statement)
    except SQLAlchemyError as exc:
        logger.warning("ai_gateway.usage_stamp_failed", model_id=str(model_id), error_class=type(exc).__name__)
    except Exception as exc:
        logger.exception("ai_gateway.usage_stamp_crashed", model_id=str(model_id), error_class=type(exc).__name__)


async def _stamp_usage(model_id: UUID, session_provider: SessionProvider) -> None:
    """Record real traffic: stamp `last_used_at` and re-arm the inactivity alert."""
    await _stamp(model_id, {"last_used_at": datetime.now(UTC), "inactivity_alerted_at": None}, session_provider)


async def _stamp_warmup(model_id: UUID, session_provider: SessionProvider) -> None:
    """Record a warmup probe, at most once per `_WARMUP_STAMP_INTERVAL`.

    Deliberately does *not* touch `last_used_at`: keeping an endpoint warm without
    messaging it is the cost pattern the inactivity alert exists to catch.

    The freshness guard keeps a cold endpoint from serialising a write per poll — clients
    poll every few seconds until `ready`, and only the most recent probe carries any
    information ("something is still waking this model").
    """
    now = datetime.now(UTC)
    await _stamp(
        model_id,
        {"last_warmup_at": now},
        session_provider,
        or_(col(AiModel.last_warmup_at).is_(None), col(AiModel.last_warmup_at) < now - _WARMUP_STAMP_INTERVAL),
    )


async def dispatch_chat(  # noqa: PLR0913 — one keyword per independent dispatch input; a carrier object would just shift the surface area
    session: AsyncSession,
    settings: Settings,
    *,
    model_alias: str,
    messages: list[ChatMessage],
    params: dict[str, Any] | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 — forwarded to the provider's own timeout
    provider: ModelProvider | None = None,
    system_suffix: str | None = None,
    session_provider: SessionProvider,
) -> ChatCompletion:
    """Resolve ``model_alias`` and return one completion from its provider.

    ``params`` merges over the row's stored ``parameters`` (call wins).
    ``provider`` overrides the adapter (tests / a future second adapter).
    ``session_provider`` opens the detached session the usage stamp commits on
    (tests inject the test session). Required, not defaulted: the right stamp
    transport depends on the process — `standalone_session` needs the API
    lifespan and silently drops stamps in a Celery worker.

    Raises:
        NotFoundError: No live, enabled model matches ``model_alias``.
        ProviderAuthError: Stored credential cannot be decrypted (unusable, so a
            terminal auth fault — not a transient outage).
        ProviderError: The provider call failed (subclass per failure mode).
    """
    model = await _resolve_model(session, model_alias)
    client = provider or _DEFAULT_PROVIDER
    call = _build_call(model, settings, messages, params, system_suffix=system_suffix)
    # Stamped after `_build_call`: a permanently misconfigured model (no endpoint, no text
    # output) must not read as "used" on every failed attempt, or its idle alert never fires.
    await _stamp_usage(model.id, session_provider)
    started = time.perf_counter()
    try:
        completion = await client.chat(**call, timeout=timeout)
    except BaseException as exc:
        _record(model.provider.value, _outcome(exc), started)
        raise
    _record(model.provider.value, "ok", started)
    return completion


async def dispatch_stream(  # noqa: PLR0913 — one keyword per independent dispatch input; a carrier object would just shift the surface area
    session: AsyncSession,
    settings: Settings,
    *,
    model_alias: str,
    messages: list[ChatMessage],
    params: dict[str, Any] | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 — forwarded to the provider's own timeout
    provider: ModelProvider | None = None,
    system_suffix: str | None = None,
    session_provider: SessionProvider,
) -> AsyncIterator[ChatChunk]:
    """Streaming counterpart of `dispatch_chat`. Await it, then iterate the result.

    Deliberately not an async generator: resolution / credential / capability
    errors surface on ``await`` (before any chunk); provider / transport errors,
    including mid-stream failures after partial output, surface while iterating.

    The usage stamp fires as iteration begins, not on the ``await``: both call
    sites `db.close()` between the two, so the stamp's pooled checkout never
    nests inside a held request connection. A config fault still raises on the
    ``await``, before any stamp — config faults are not usage.
    """
    model = await _resolve_model(session, model_alias)
    call = _build_call(model, settings, messages, params, system_suffix=system_suffix)
    client = provider or _DEFAULT_PROVIDER
    stream = client.stream(**call, timeout=timeout)
    return _stamped_stream(_observed_stream(stream, model.provider.value), model.id, session_provider)


async def _stamped_stream(
    stream: AsyncIterator[ChatChunk], model_id: UUID, session_provider: SessionProvider
) -> AsyncIterator[ChatChunk]:
    await _stamp_usage(model_id, session_provider)
    try:
        async for chunk in stream:
            yield chunk
    finally:
        # `async for` does not close the inner generator on early exit — without this,
        # a consumer close never reaches `_observed_stream`'s metrics/cleanup finally.
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


# Probe fault → (readiness, machine reason). RateLimit is alive-but-throttled; unreachable/timeout are
# transient (retry, waiting out a cold start); the rest are terminal. Anything unmapped → generic error.
# A `READY` fault is reachable-but-not-served. The reason is never persisted (an alive row carries
# none) — its *presence* is the signal `run_health_check` reads to skip the capability probe, whose
# differential premise needs the text probe to have actually been served. Any future `(READY, …)`
# entry inherits that, which is the intended default.
_THROTTLED_REASON = "rate-limit"

_HEALTH_FAULTS: dict[type[ProviderError], tuple[WarmupStatus, str | None]] = {
    ProviderRateLimitError: (WarmupStatus.READY, _THROTTLED_REASON),
    ProviderTimeoutError: (WarmupStatus.STARTING, "timeout"),
    ProviderUnavailableError: (WarmupStatus.STARTING, "unreachable"),
    ProviderContextWindowError: (WarmupStatus.ERROR, "context-window"),
    ProviderAuthError: (WarmupStatus.ERROR, "auth"),
    ProviderBadRequestError: (WarmupStatus.ERROR, "bad-request"),
}


async def dispatch_health(
    model: AiModel,
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
) -> tuple[WarmupStatus, str | None]:
    """One health probe on an already-resolved row → (readiness, machine reason).

    Operates on the row directly (via `_build_call`), so it works on a disabled model — the manual
    health check must be usable before enabling. Never records metrics (not real traffic) and never
    raises `ProviderError`: every fault maps via `_HEALTH_FAULTS` to a status + short reason. `READY` =
    alive; `STARTING` is transient (the caller retries, waiting out a cold start); `ERROR` is terminal
    dead. Shared classifier behind `dispatch_probe` (which drops the reason).
    """
    client = provider or _DEFAULT_PROVIDER
    try:
        call = _build_call(model, settings, [_PROBE_MESSAGE], None)
        # Cap after the build, not through `params`: the cap is ours, and an
        # `advanced_params_disabled` row drops everything the caller passes as params.
        call["params"] = {**call["params"], "max_tokens": 1}
        await client.chat(**call, timeout=_PROBE_TIMEOUT)
    except ProviderError as exc:
        return _HEALTH_FAULTS.get(type(exc), (WarmupStatus.ERROR, "error"))
    return WarmupStatus.READY, None


async def dispatch_capability_probe(
    model: AiModel,
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
) -> tuple[bool, str | None]:
    """Check the row's declared image input against the endpoint → (conclusive, mismatch reason).

    Three outcomes, not two: `(True, None)` the endpoint took the image, `(True, reason)`
    it refused it, `(False, None)` the probe learned nothing. The caller must keep the
    stored finding on the third — collapsing it into "no mismatch" would report a
    rate-limited endpoint as having confirmed a declaration nobody checked.

    Only `ProviderBadRequestError` is conclusive against the declaration, and it is
    conclusive because the probe is differential: `dispatch_health` has just made the
    identical call *minus* the image part and **been served**, so a 400 on this one is
    attributable to the image or the content-part shape. The caller owes that premise —
    a liveness verdict that came from a rate limit does not carry it. Every other fault says nothing
    either way — auth, rate limits, timeouts, outages, an overflowed context window, and anything
    `_map_error` could not classify, which degrades to the base `ProviderError`. Every inconclusive
    outcome is logged with its `error_class`; the bare base class is the one worth hunting for.

    The reason carries the upstream text rather than a verdict: an endpoint may reject
    the `data:` URI or the multimodal content shape while the model behind it does have
    vision, and the operator, not this probe, is the one who can tell the difference.
    """
    client = provider or _DEFAULT_PROVIDER
    # Built outside the `try` so only the provider's refusal can be reported as one:
    # our own gates also raise ProviderBadRequestError, and dressing those as upstream
    # evidence would accuse the operator of what the caller got wrong.
    call = _build_call(model, settings, [_PROBE_IMAGE_MESSAGE], None)
    # The cap is the gateway's kwarg, not the operator's: ride `params` and a model that opts
    # out of the cascade drops it, turning the probe into a full, billable reply.
    call["params"] = {**call["params"], "max_tokens": 1}
    try:
        await client.chat(**call, timeout=_PROBE_TIMEOUT)
    except ProviderBadRequestError as exc:
        # Marked when cut: the column holds 255 chars and a minified provider error clears that
        # routinely, so a bare slice hands the operator a severed token they cannot tell from the
        # whole message — and the console quotes this string verbatim.
        reason = str(exc)
        return True, reason if len(reason) <= MISMATCH_MAX_LEN else f"{reason[: MISMATCH_MAX_LEN - 1]}…"
    except ProviderError as exc:
        # Inconclusive writes nothing, so without this line a billable call and up to
        # `_PROBE_TIMEOUT` leave no trace anywhere. Worth a log rather than a column: an
        # unrecognised failure on a multimodal payload arrives here as the bare base class, and
        # that is the signal this probe exists to find.
        logger.info("capability_probe.inconclusive", model_id=str(model.id), error_class=type(exc).__name__)
        return False, None
    return True, None


async def dispatch_probe(
    session: AsyncSession,
    settings: Settings,
    *,
    model_alias: str,
    provider: ModelProvider | None = None,
    session_provider: SessionProvider,
) -> WarmupStatus:
    """Probe a model with a 1-token chat and classify readiness — never raises `ProviderError`.

    For a scale-to-zero endpoint the probe *is* the wake signal: a cold endpoint
    fails the call (which triggers scale-up) and maps to ``starting``. A reachable
    endpoint — even one rate-limiting — is ``ready``; a terminal fault (auth, bad
    request, a model that cannot answer in text) is ``error``.

    Raises:
        NotFoundError: No live, enabled model matches ``model_alias`` — a 404, not
            a warmup state, so it is not swallowed into `error`.
    """
    model = await _resolve_model(session, model_alias)
    await _stamp_warmup(model.id, session_provider)
    status, _reason = await dispatch_health(model, settings, provider=provider)
    return status


def _build_call(
    model: AiModel,
    settings: Settings,
    messages: list[ChatMessage],
    params: dict[str, Any] | None,
    *,
    system_suffix: str | None = None,
) -> dict[str, Any]:
    """Provider-call kwargs (minus timeout) from a resolved model.

    Validates declared modalities and the base URL, resolves the credential, merges params (call over row),
    and translates registry-named knobs to litellm's: `system_prompt` becomes a
    prepended system message, `stop_sequences` becomes `stop`. `extras` is
    metadata and is not forwarded.

    An `advanced_params_disabled` model drops the whole operator-set cascade here — the row's
    own knobs and everything the caller merged on top. This is the single point every provider
    call funnels through, so the flag cannot be bypassed by adding a param source upstream.

    `system_suffix` is appended to the system message *after* that suppression: it carries
    gateway-owned context (the conversation's tag block) which is not an operator knob and must
    still reach the model on a flagged row — otherwise a reply would record tag context its
    prompt never carried. Keep it out of `params` for exactly that reason.
    """
    if Modality.TEXT not in model.output_modalities:
        raise ProviderBadRequestError(f"Model {model.model_alias!r} does not produce text output.")
    if Modality.IMAGE not in model.input_modalities and _has_image_content(messages):
        raise ProviderBadRequestError(f"Model {model.model_alias!r} does not accept image input.")
    # Write-side validation cannot reach rows that predate it. Without this, such a row
    # rides the `openai` prefix with `api_base=None` and no credential of its own — i.e.
    # litellm's OpenAI defaults, red-teaming OpenAI on the platform key.
    if missing_inference_endpoint(model.provider, model.inference_endpoint):
        raise ProviderBadRequestError(
            f"Model {model.model_alias!r} is generic but carries no inference_endpoint — there is no base URL to call."
        )
    merged = {} if model.advanced_params_disabled else merge_inference_params(model.parameters, params)
    system_prompt = merged.pop("system_prompt", None)
    # Registry names it stop_sequences; litellm's kwarg is stop. Without this it
    # would be dropped by drop_params and silently never applied.
    stop = merged.pop("stop_sequences", None)
    if stop is not None:
        merged.setdefault("stop", stop)
    system_content = "\n\n".join(part for part in (system_prompt, system_suffix) if part)
    call_messages = [ChatMessage(role="system", content=system_content), *messages] if system_content else messages
    return {
        "vendor": model.provider,
        "provider_model_id": model.provider_model_id,
        "messages": call_messages,
        "api_key": _credential(model, settings),
        "api_base": model.inference_endpoint,
        "params": merged,
    }


def _has_image_content(messages: list[ChatMessage]) -> bool:
    return any(
        isinstance(m.content, list) and any(isinstance(part, ImageContentPart) for part in m.content) for m in messages
    )


async def _resolve_model(session: AsyncSession, model_alias: str) -> AiModel:
    statement = AiModel.live_select().where(col(AiModel.model_alias) == model_alias)
    result = await session.execute(statement)
    model = result.scalar_one_or_none()
    if model is None or model.is_disabled:
        raise NotFoundError(f"Model {model_alias!r} is not available.")
    return model


def _credential(model: AiModel, settings: Settings) -> str | None:
    try:
        secret = resolve_api_key(model, settings)
    except SecretDecryptError as exc:
        raise ProviderAuthError(f"Stored credential for model {model.model_alias!r} could not be decrypted.") from exc
    return secret.get_secret_value() if secret is not None else None
