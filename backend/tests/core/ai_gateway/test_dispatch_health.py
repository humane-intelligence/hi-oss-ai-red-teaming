from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.dispatch import _HEALTH_FAULTS
from app.core.ai_gateway.dispatch import dispatch_health
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.enums import WarmupStatus
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import create_model
from app.core.config import Settings


class _RecordingProvider:
    """Succeeds — drives the alive path."""

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        return ChatCompletion(
            id="x",
            model="m",
            created=1,
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="ok"))],
        )

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
        yield  # pragma: no cover


class _RaisingProvider:
    """Raises a fixed exception from `chat`, to exercise the classifier."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        raise self._exc

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
        yield  # pragma: no cover


async def _model(session: AsyncSession, settings: Settings) -> AiModel:
    return await create_model(
        session,
        settings,
        name="Health",
        model_alias="health-probe",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://slm:8080/v1",
    )


@pytest.mark.integration
async def test_dispatch_health_ready_no_reason(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _model(db_session, app_settings)
    assert await dispatch_health(model, app_settings, provider=_RecordingProvider()) == (WarmupStatus.READY, None)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # Alive, but the reason is not `None`: a throttle means the probe was never served, which is
        # what stops `run_health_check` from billing a capability probe against it. The reason is
        # never persisted — an alive row carries none (see `test_health.py`).
        (ProviderRateLimitError("429"), (WarmupStatus.READY, "rate-limit")),
        (ProviderUnavailableError("down"), (WarmupStatus.STARTING, "unreachable")),
        (ProviderTimeoutError("slow"), (WarmupStatus.STARTING, "timeout")),
        (ProviderContextWindowError("ctx"), (WarmupStatus.ERROR, "context-window")),
        (ProviderAuthError("401"), (WarmupStatus.ERROR, "auth")),
        (ProviderBadRequestError("bad"), (WarmupStatus.ERROR, "bad-request")),
        (ProviderError("boom"), (WarmupStatus.ERROR, "error")),
    ],
)
async def test_dispatch_health_maps_errors(
    db_session: AsyncSession, app_settings: Settings, exc: Exception, expected: tuple[WarmupStatus, str | None]
) -> None:
    model = await _model(db_session, app_settings)
    assert await dispatch_health(model, app_settings, provider=_RaisingProvider(exc)) == expected


@pytest.mark.unit
def test_every_alive_fault_carries_a_reason() -> None:
    # `run_health_check` reads the presence of a reason on a `READY` verdict as "not served" and
    # skips the capability probe. A `(READY, None)` entry would silently start billing a second
    # call against an endpoint that just refused the first.
    alive = {exc: reason for exc, (status, reason) in _HEALTH_FAULTS.items() if status is WarmupStatus.READY}

    assert alive
    assert all(reason is not None for reason in alive.values())
