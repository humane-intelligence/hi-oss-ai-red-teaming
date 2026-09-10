"""Integration tests for dispatch_probe — the readiness classification over a real registry row.

A fake provider stands in for litellm: a recording one for the success path and a
raising one to drive each `ProviderError` subtype through the classifier.
"""

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
from app.core.ai_gateway.crypto import SecretDecryptError
from app.core.ai_gateway.dispatch import _PROBE_TIMEOUT
from app.core.ai_gateway.dispatch import dispatch_probe
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.enums import WarmupStatus
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
    """Succeeds, capturing the kwargs the probe passed through."""

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}

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
        self.last = {"params": params, "timeout": timeout, "messages": messages}
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
        raise NotImplementedError  # unused: the probe only calls chat
        yield  # pragma: no cover — makes this an async generator to satisfy the port


class _RaisingProvider:
    """Raises a fixed exception from `chat`, to exercise the error classifier."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        raise self._exc

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
        yield  # pragma: no cover


async def _create_generic(session: AsyncSession, settings: Settings, **overrides: Any) -> None:
    await create_model(
        session,
        settings,
        name="Probe",
        model_alias="probe-alias",
        provider=ProviderVendor.GENERIC,
        provider_model_id="probe-model",
        inference_endpoint="http://slm:8080/v1",
        **overrides,
    )


@pytest.mark.integration
async def test_probe_ready_on_success(db_session: AsyncSession, app_settings: Settings) -> None:
    await _create_generic(db_session, app_settings)

    result = await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )

    assert result is WarmupStatus.READY


@pytest.mark.integration
async def test_probe_caps_tokens_and_uses_short_timeout(db_session: AsyncSession, app_settings: Settings) -> None:
    await _create_generic(db_session, app_settings)
    fake = _RecordingProvider()

    await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["params"]["max_tokens"] == 1
    assert fake.last["timeout"] == _PROBE_TIMEOUT


@pytest.mark.integration
async def test_probe_cap_wins_over_the_rows_own_max_tokens(db_session: AsyncSession, app_settings: Settings) -> None:
    # The cap is applied after the build, so it has to beat a row that sets its own value —
    # otherwise a probe against a large-output model costs a full generation.
    await _create_generic(db_session, app_settings, parameters={"max_tokens": 4096})
    fake = _RecordingProvider()

    await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["params"]["max_tokens"] == 1


@pytest.mark.integration
async def test_probe_keeps_its_cap_on_a_params_disabled_model(db_session: AsyncSession, app_settings: Settings) -> None:
    # The opt-out drops operator knobs, not the gateway's own: a flagged model must still
    # be probeable (the manual health check has to work before enabling a model).
    await _create_generic(db_session, app_settings, parameters={"temperature": 0.5}, advanced_params_disabled=True)
    fake = _RecordingProvider()

    await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=fake,
        session_provider=_session_provider(db_session),
    )

    assert fake.last["params"] == {"max_tokens": 1}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderUnavailableError("down"), WarmupStatus.STARTING),
        (ProviderTimeoutError("slow"), WarmupStatus.STARTING),
        (ProviderRateLimitError("429"), WarmupStatus.READY),
        (ProviderAuthError("401"), WarmupStatus.ERROR),
        (ProviderBadRequestError("400"), WarmupStatus.ERROR),
        (ProviderContextWindowError("ctx"), WarmupStatus.ERROR),
        (ProviderError("unclassified"), WarmupStatus.ERROR),
    ],
)
async def test_probe_classifies_provider_errors(
    db_session: AsyncSession, app_settings: Settings, exc: Exception, expected: WarmupStatus
) -> None:
    await _create_generic(db_session, app_settings)

    result = await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=_RaisingProvider(exc),
        session_provider=_session_provider(db_session),
    )

    assert result is expected


@pytest.mark.integration
async def test_probe_model_without_text_output_is_error(db_session: AsyncSession, app_settings: Settings) -> None:
    # `_build_call` rejects a model that cannot answer in text with `ProviderBadRequestError` before the
    # provider is called — the probe classifies that as a terminal `error`, not `starting`.
    await create_model(
        db_session,
        app_settings,
        name="Imager",
        model_alias="imager-probe",
        provider=ProviderVendor.OPENAI,
        provider_model_id="dall-e-3",
        output_modalities=[Modality.IMAGE],
    )

    result = await dispatch_probe(
        db_session,
        app_settings,
        model_alias="imager-probe",
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )

    assert result is WarmupStatus.ERROR


@pytest.mark.integration
async def test_probe_undecryptable_credential_is_error(
    db_session: AsyncSession, app_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An undecryptable stored credential (e.g. the at-rest key rotated) is raised
    # pre-provider by `_credential` as a terminal `ProviderAuthError` — the probe must
    # classify it `error`, not spin on `starting` until the client's poll cap.
    await _create_generic(db_session, app_settings)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise SecretDecryptError("key rotated")

    monkeypatch.setattr("app.core.ai_gateway.dispatch.resolve_api_key", _raise)

    result = await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )

    assert result is WarmupStatus.ERROR


@pytest.mark.integration
async def test_probe_unknown_alias_propagates_not_found(db_session: AsyncSession, app_settings: Settings) -> None:
    # A missing model is a 404 (an ops/config error), not a warmup state — it must not
    # be swallowed into `error`.
    with pytest.raises(NotFoundError):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="does-not-exist",
            provider=_RecordingProvider(),
            session_provider=_session_provider(db_session),
        )


def _calls_total(outcome: str) -> float:
    return REGISTRY.get_sample_value("redteam_model_calls_total", {"provider": "generic", "outcome": outcome}) or 0.0


@pytest.mark.integration
async def test_probe_does_not_record_gateway_metrics(db_session: AsyncSession, app_settings: Settings) -> None:
    # Warmup probes aren't real traffic — recording them would pollute provider
    # error-rate/latency series with cold-start timeouts.
    await _create_generic(db_session, app_settings)
    before = _calls_total("ok")

    await dispatch_probe(
        db_session,
        app_settings,
        model_alias="probe-alias",
        provider=_RecordingProvider(),
        session_provider=_session_provider(db_session),
    )

    assert _calls_total("ok") == before
