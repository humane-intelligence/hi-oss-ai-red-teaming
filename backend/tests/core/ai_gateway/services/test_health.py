import uuid
from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.enums import HealthCheckStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderUnavailableError
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import create_model
from app.core.ai_gateway.services.health import run_health_check
from app.core.ai_gateway.services.health import start_health_check
from app.core.config import Settings
from app.core.config import get_settings


@pytest.fixture
def app_settings() -> Settings:
    # Deliberately shadows the package fixture: backoff 0 so the retry loop doesn't actually sleep.
    return get_settings().model_copy(update={"health_check_backoff_seconds": 0})


def _completion() -> ChatCompletion:
    return ChatCompletion(
        id="x",
        model="m",
        created=1,
        choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="ok"))],
    )


class _ScriptedProvider:
    """Raises each queued exception in turn; a `None` entry succeeds."""

    def __init__(self, script: list[Exception | None]) -> None:
        self._script = list(script)

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        step = self._script.pop(0) if self._script else None
        if step is not None:
            raise step
        return _completion()

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
        yield  # pragma: no cover


async def _model(
    session: AsyncSession,
    settings: Settings,
    *,
    input_modalities: list[Modality] | None = None,
    provider: ProviderVendor = ProviderVendor.GENERIC,
    provider_model_id: str = "m",
    inference_endpoint: str | None = "http://slm:8080/v1",
) -> AiModel:
    model = await create_model(
        session,
        settings,
        name="Health",
        model_alias="health-svc",
        provider=provider,
        provider_model_id=provider_model_id,
        inference_endpoint=inference_endpoint,
        input_modalities=input_modalities,
    )
    await start_health_check(session, model)
    return model


@pytest.mark.integration
async def test_new_model_has_null_health_fields(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="Fresh",
        model_alias="fresh",
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )
    assert model.health_check_status is None
    assert model.last_health_check_at is None
    assert model.last_health_reason is None
    assert model.last_healthy_at is None


@pytest.mark.integration
async def test_start_health_check_marks_checking(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _model(db_session, app_settings)
    assert model.health_check_status is HealthCheckStatus.CHECKING
    assert model.last_health_check_at is not None


@pytest.mark.integration
async def test_starting_then_ready_settles_alive(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _model(db_session, app_settings)
    provider = _ScriptedProvider([ProviderUnavailableError("cold"), ProviderUnavailableError("cold"), None])
    await run_health_check(db_session, app_settings, model.id, provider=provider)
    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.ALIVE
    assert model.last_health_reason is None
    assert model.last_healthy_at is not None


@pytest.mark.integration
async def test_terminal_error_settles_dead_with_reason(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _model(db_session, app_settings)
    await run_health_check(db_session, app_settings, model.id, provider=_ScriptedProvider([ProviderAuthError("401")]))
    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.DEAD
    assert model.last_health_reason == "auth"
    assert model.last_healthy_at is None


@pytest.mark.integration
async def test_deadline_while_starting_settles_dead_cold(db_session: AsyncSession, app_settings: Settings) -> None:
    settings = app_settings.model_copy(update={"health_check_wake_deadline_seconds": 0})  # immediate deadline
    model = await _model(db_session, settings)
    await run_health_check(
        db_session, settings, model.id, provider=_ScriptedProvider([ProviderUnavailableError("cold")])
    )
    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.DEAD
    assert model.last_health_reason == "cold/deadline"


@pytest.mark.integration
async def test_cas_superseded_run_does_not_clobber(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _model(db_session, app_settings)

    class _BumpThenSucceed:
        """Simulates a newer check overriding the row mid-probe, so this run's CAS settle finds 0 rows."""

        async def chat(self, **_kwargs: Any) -> ChatCompletion:
            model.last_health_check_at = (model.last_health_check_at or datetime.now(UTC)) + timedelta(seconds=1)
            await db_session.flush()
            return _completion()

        async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
            raise NotImplementedError
            yield  # pragma: no cover

    await run_health_check(db_session, app_settings, model.id, provider=_BumpThenSucceed())
    await db_session.refresh(model)
    # The settle targeted the pre-bump token, matched 0 rows — status stays CHECKING, not flipped to ALIVE.
    assert model.health_check_status is HealthCheckStatus.CHECKING
    assert model.last_healthy_at is None


@pytest.mark.integration
async def test_capability_probe_records_a_mismatch_without_touching_health(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The endpoint answers the text probe and refuses the image one: alive, but the
    # row's claim is wrong. Conflating the two would send the operator to fix the
    # endpoint when the thing to fix is the declaration.
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])
    provider = _ScriptedProvider([None, ProviderBadRequestError("image_url not supported")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.ALIVE
    assert model.last_health_reason is None
    assert model.capability_mismatch is not None
    assert "image_url not supported" in model.capability_mismatch


@pytest.mark.integration
async def test_capability_probe_skipped_when_the_row_claims_no_image_input(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Nothing to contradict, so the probe must not spend a second provider call.
    model = await _model(db_session, app_settings)
    provider = _ScriptedProvider([None, ProviderBadRequestError("would be a false alarm")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.ALIVE
    assert model.capability_mismatch is None


@pytest.mark.integration
async def test_a_dead_endpoint_neither_probes_nor_discards_the_previous_finding(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])
    # Seeded, because an empty column cannot tell "left alone" from "cleared" — and a
    # check that never asked must not answer. Health checks are manual, so a wrongly
    # cleared finding is gone until a human runs another one.
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()
    # Second entry is the tripwire: if the probe ran despite the endpoint being dead,
    # it would raise this and overwrite the finding with its own text.
    provider = _ScriptedProvider([ProviderAuthError("401"), ProviderBadRequestError("probe should not run")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.DEAD
    assert model.capability_mismatch == "unsupported content type: image_url"


@pytest.mark.integration
async def test_an_inconclusive_probe_keeps_the_previous_finding(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Alive endpoint, but the capability probe itself hits a reachability fault. That is
    # the fault set the classifier refuses to read as a mismatch; reading it as *clean*
    # is the same error mirrored, and it would erase a finding nothing re-examined.
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()
    provider = _ScriptedProvider([None, ProviderRateLimitError("slow down")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.ALIVE
    assert model.capability_mismatch == "unsupported content type: image_url"


@pytest.mark.integration
async def test_a_passing_probe_clears_a_previous_mismatch(db_session: AsyncSession, app_settings: Settings) -> None:
    # The column is the last check's result, not sediment from an older one.
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])
    model.capability_mismatch = "stale finding"
    await db_session.flush()

    await run_health_check(db_session, app_settings, model.id, provider=_ScriptedProvider([None, None]))

    await db_session.refresh(model)
    assert model.capability_mismatch is None


@pytest.mark.integration
async def test_settle_does_not_restamp_a_finding_the_operator_already_fixed(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    """A PATCH dropping `image` mid-check must not be undone by that check's settle.

    Clearing the declaration is the remedy a finding prescribes, and it leaves the CAS
    token untouched — so without the declaration guard the settle would re-stamp a
    finding about a claim the row no longer makes, and only another manual health check
    could clear it.
    """
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])

    class _PatchesMidCheck(_ScriptedProvider):
        """Drops `image` from the row between the text probe and the capability probe."""

        def __init__(self, session: AsyncSession, model_id: uuid.UUID) -> None:
            super().__init__([None, ProviderBadRequestError("image_url not supported")])
            self._session = session
            self._model_id = model_id
            self._served_text = False

        async def chat(self, **kwargs: Any) -> ChatCompletion:
            # Patch on the *second* call — the capability probe. Patching on the first would
            # synchronise `input_modalities` on the in-session instance before `run_health_check`
            # reads it, so the probe would be skipped and this test would pass without the guard.
            if not self._served_text:
                self._served_text = True
                return await super().chat(**kwargs)
            await self._session.execute(
                update(AiModel)
                .where(col(AiModel.id) == self._model_id)
                .values(input_modalities=[Modality.TEXT], capability_mismatch=None)
            )
            await self._session.commit()
            return await super().chat(**kwargs)

    await run_health_check(db_session, app_settings, model.id, provider=_PatchesMidCheck(db_session, model.id))

    await db_session.refresh(model)
    assert model.input_modalities == [Modality.TEXT]
    assert model.capability_mismatch is None
    # The health half of the settle still lands — only the capability write is guarded.
    assert model.health_check_status is HealthCheckStatus.ALIVE


@pytest.mark.integration
async def test_a_throttled_endpoint_is_alive_but_not_probed(db_session: AsyncSession, app_settings: Settings) -> None:
    # A 429 means the text probe was never served, so the capability probe has no baseline to be
    # differential against — and billing a second call to ask a question the throttle just refused
    # would be the wrong answer twice over. Second entry is the tripwire.
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])
    provider = _ScriptedProvider([ProviderRateLimitError("429"), ProviderBadRequestError("probe should not run")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.health_check_status is HealthCheckStatus.ALIVE
    # Alive rows carry no reason: the throttle marker exists only to gate the probe.
    assert model.last_health_reason is None
    assert model.capability_mismatch is None


@pytest.mark.integration
async def test_settle_does_not_attribute_a_finding_to_a_replaced_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # A finding is evidence about a (declaration, endpoint) pair. If the endpoint moves mid-check,
    # the old endpoint's refusal must not be stamped as the new one's.
    model = await _model(db_session, app_settings, input_modalities=[Modality.TEXT, Modality.IMAGE])

    class _MovesEndpointMidCheck(_ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([None, ProviderBadRequestError("old endpoint refused the image")])
            self._moved = False

        async def chat(self, **kwargs: Any) -> ChatCompletion:
            if not self._moved:
                self._moved = True
                return await super().chat(**kwargs)
            await db_session.execute(
                update(AiModel)
                .where(col(AiModel.id) == model.id)
                .values(inference_endpoint="http://new:8080/v1", capability_mismatch=None)
            )
            await db_session.commit()
            return await super().chat(**kwargs)

    await run_health_check(db_session, app_settings, model.id, provider=_MovesEndpointMidCheck())

    await db_session.refresh(model)
    assert model.inference_endpoint == "http://new:8080/v1"
    assert model.capability_mismatch is None
    # The health half is about the endpoint being reachable, so it still settles.
    assert model.health_check_status is HealthCheckStatus.ALIVE


@pytest.mark.integration
async def test_settle_does_not_attribute_a_finding_to_a_replaced_model_id(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The same misattribution reached through the model-id half of the probed call: `_build_call`
    # sends `provider_model_id`, so gpt-3.5's refusal must not be stored as gpt-4o's finding.
    model = await _model(
        db_session,
        app_settings,
        input_modalities=[Modality.TEXT, Modality.IMAGE],
        provider_model_id="gpt-3.5-turbo",
    )

    class _MovesModelIdMidCheck(_ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([None, ProviderBadRequestError("gpt-3.5 refused the image")])
            self._served_text = False

        async def chat(self, **kwargs: Any) -> ChatCompletion:
            if not self._served_text:
                self._served_text = True
                return await super().chat(**kwargs)
            await db_session.execute(
                update(AiModel).where(col(AiModel.id) == model.id).values(provider_model_id="gpt-4o")
            )
            await db_session.commit()
            return await super().chat(**kwargs)

    await run_health_check(db_session, app_settings, model.id, provider=_MovesModelIdMidCheck())

    await db_session.refresh(model)
    assert model.provider_model_id == "gpt-4o"
    assert model.capability_mismatch is None
    assert model.health_check_status is HealthCheckStatus.ALIVE


@pytest.mark.integration
async def test_a_finding_is_written_for_a_row_with_no_inference_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Most of the registry (`openai`/`anthropic`/`azure`) carries a NULL endpoint, and the settle
    # guard compares against it. It works only because SQLAlchemy coerces a runtime `None` into
    # `IS NULL`; without this test a refactor to an explicit bindparam would silently stop
    # persisting findings for those rows.
    model = await _model(
        db_session,
        app_settings,
        input_modalities=[Modality.TEXT, Modality.IMAGE],
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        inference_endpoint=None,
    )
    provider = _ScriptedProvider([None, ProviderBadRequestError("unsupported content type: image_url")])

    await run_health_check(db_session, app_settings, model.id, provider=provider)

    await db_session.refresh(model)
    assert model.inference_endpoint is None
    assert model.capability_mismatch == "unsupported content type: image_url"
