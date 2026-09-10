"""Unit tests for `dispatch_capability_probe` — the classifier, not the transport.

The probe's whole job is deciding which provider faults are evidence that the row's
declaration is wrong. A rate-limited endpoint must not get its model accused of lying
about vision — nor, and this is the second half, credited with having confirmed it:
the third state (`conclusive=False`) is what keeps the caller from clearing a finding
no probe re-examined.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.dispatch import dispatch_capability_probe
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.models import AiModel
from app.core.config import Settings


def _vision_model() -> AiModel:
    return AiModel(
        name="Vision",
        model_alias="vision",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
        output_modalities=[Modality.TEXT],
    )


class _Provider:
    """Raises `fault` if given one, otherwise answers and records the call."""

    def __init__(self, fault: ProviderError | None = None) -> None:
        self.fault = fault
        self.last: dict[str, Any] = {}

    async def chat(self, **kwargs: Any) -> ChatCompletion:
        self.last = kwargs
        if self.fault is not None:
            raise self.fault
        return ChatCompletion(
            id="x",
            model="m",
            created=1,
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="ok"))],
        )

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError  # unused: the probe only calls chat
        yield  # pragma: no cover — makes this an async generator to satisfy the port


@pytest.mark.unit
async def test_probe_reports_a_mismatch_only_on_bad_request(app_settings: Settings) -> None:
    provider = _Provider(ProviderBadRequestError("unsupported content type: image_url"))

    conclusive, reason = await dispatch_capability_probe(_vision_model(), app_settings, provider=provider)

    assert conclusive
    assert reason == "unsupported content type: image_url"


@pytest.mark.unit
@pytest.mark.parametrize(
    "fault",
    [
        ProviderAuthError("bad key"),
        ProviderRateLimitError("slow down"),
        ProviderTimeoutError("timeout"),
        ProviderUnavailableError("502"),
    ],
)
async def test_probe_is_inconclusive_on_reachability_faults(app_settings: Settings, fault: ProviderError) -> None:
    provider = _Provider(fault)

    # Inconclusive, not clean: the caller keys "leave the stored finding alone" off the
    # first element, so a fault reported as `(True, None)` would erase it.
    assert await dispatch_capability_probe(_vision_model(), app_settings, provider=provider) == (False, None)


@pytest.mark.unit
async def test_an_inconclusive_probe_is_logged(app_settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    # Nothing is written on this path, and the settle log renders it identically to a check that
    # never probed — so the log line is the only trace that a billable call was spent.
    provider = _Provider(ProviderError("gateway returned 413 for the multipart body"))

    with caplog.at_level(logging.INFO):
        assert await dispatch_capability_probe(_vision_model(), app_settings, provider=provider) == (False, None)

    assert "capability_probe.inconclusive" in caplog.text
    assert "ProviderError" in caplog.text


@pytest.mark.unit
async def test_probe_reports_nothing_when_the_endpoint_takes_the_image(app_settings: Settings) -> None:
    provider = _Provider()

    assert await dispatch_capability_probe(_vision_model(), app_settings, provider=provider) == (True, None)


@pytest.mark.unit
async def test_probe_sends_an_image_part_capped_at_one_token(app_settings: Settings) -> None:
    provider = _Provider()
    # A row generous with its own cap: the probe's 1 has to win, or a single check
    # bills a full reply.
    model = _vision_model()
    model.parameters = {"max_tokens": 4096}

    await dispatch_capability_probe(model, app_settings, provider=provider)

    content = provider.last["messages"][0].content
    assert any(isinstance(part, ImageContentPart) for part in content)
    assert provider.last["params"]["max_tokens"] == 1


@pytest.mark.unit
async def test_probe_reason_fits_the_column(app_settings: Settings) -> None:
    # `capability_mismatch` is varchar(255); an upstream message is not bounded.
    provider = _Provider(ProviderBadRequestError("x" * 4000))

    _conclusive, reason = await dispatch_capability_probe(_vision_model(), app_settings, provider=provider)

    assert reason is not None
    assert len(reason) == 255
    # Marked, not silently severed — the console quotes this string as if it were complete.
    assert reason.endswith("…")
