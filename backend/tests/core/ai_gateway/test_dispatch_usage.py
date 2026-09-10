"""Integration tests for the inactivity bookkeeping dispatch writes.

Real traffic (`dispatch_chat` / `dispatch_stream`) stamps `last_used_at` and re-arms the
alert; a warmup probe stamps only `last_warmup_at` — keeping an endpoint warm without
messaging it is the cost pattern the alert exists to catch, so it must not reset the clock.
"""

from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.dispatch import dispatch_chat
from app.core.ai_gateway.dispatch import dispatch_probe
from app.core.ai_gateway.dispatch import dispatch_stream
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import create_model
from app.core.config import Settings

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class _StubProvider:
    """Answers `chat` and `stream` with a canned reply — no network, no litellm."""

    async def chat(self, **_kwargs: Any) -> ChatCompletion:
        return ChatCompletion(
            id="x",
            model="m",
            created=1,
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="ok"))],
        )

    async def stream(self, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(
            id="x",
            model="m",
            created=1,
            choices=[ChunkChoice(index=0, delta=ChatMessageDelta(content="ok"), finish_reason="stop")],
        )


def _session_provider(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """A stamp-session provider yielding the test session (no commit/close — the fixture owns it)."""

    @asynccontextmanager
    async def provider() -> AsyncIterator[AsyncSession]:
        yield session

    return provider


async def _create_model(session: AsyncSession, settings: Settings) -> AiModel:
    return await create_model(
        session,
        settings,
        name="Warm",
        model_alias="warm-alias",
        provider=ProviderVendor.GENERIC,
        provider_model_id="warm-model",
        inference_endpoint="http://slm:8080/v1",
        warmup_enabled=True,
    )


@pytest.mark.integration
async def test_a_config_fault_does_not_stamp_usage(db_session: AsyncSession, app_settings: Settings) -> None:
    # A model that can never be dispatched to must keep alerting as idle — stamping on
    # every failed attempt would suppress exactly the registry clutter the alert surfaces.
    model = await create_model(
        db_session,
        app_settings,
        name="Imageonly",
        model_alias="image-only",
        provider=ProviderVendor.GENERIC,
        provider_model_id="image-model",
        inference_endpoint="http://slm:8080/v1",
        warmup_enabled=True,
        output_modalities=[Modality.IMAGE],
    )

    with pytest.raises(ProviderBadRequestError):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="image-only",
            messages=[ChatMessage(role="user", content="hi")],
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)

    assert model.last_used_at is None


@pytest.mark.integration
async def test_dispatch_chat_stamps_last_used_at(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _create_model(db_session, app_settings)

    with time_machine.travel(_NOW, tick=False):
        await dispatch_chat(
            db_session,
            app_settings,
            model_alias="warm-alias",
            messages=[ChatMessage(role="user", content="hi")],
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)

    assert model.last_used_at == _NOW
    assert model.last_warmup_at is None


@pytest.mark.integration
async def test_dispatch_stream_stamps_last_used_at_on_iteration(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Not on the `await`: both call sites close the request session between the await and
    # the first chunk, so the stamp's checkout must not nest inside a held connection.
    model = await _create_model(db_session, app_settings)

    with time_machine.travel(_NOW, tick=False):
        stream = await dispatch_stream(
            db_session,
            app_settings,
            model_alias="warm-alias",
            messages=[ChatMessage(role="user", content="hi")],
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
        await db_session.refresh(model)
        assert model.last_used_at is None
        async for _ in stream:
            pass
    await db_session.refresh(model)

    assert model.last_used_at == _NOW


@pytest.mark.integration
async def test_usage_rearms_the_inactivity_alert(db_session: AsyncSession, app_settings: Settings) -> None:
    # A model that already alerted must alert again after a fresh quiet period.
    model = await _create_model(db_session, app_settings)
    model.inactivity_alerted_at = _NOW - timedelta(days=1)
    db_session.add(model)
    await db_session.flush()

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="warm-alias",
        messages=[ChatMessage(role="user", content="hi")],
        provider=_StubProvider(),
        session_provider=_session_provider(db_session),
    )
    await db_session.refresh(model)

    assert model.inactivity_alerted_at is None


@pytest.mark.integration
async def test_probe_stamps_warmup_only(db_session: AsyncSession, app_settings: Settings) -> None:
    # Warmup fires on every conversation open; counting it as usage would suppress the
    # alert forever on exactly the endpoints it is meant to catch.
    model = await _create_model(db_session, app_settings)
    alerted_at = _NOW - timedelta(days=1)
    model.inactivity_alerted_at = alerted_at
    db_session.add(model)
    await db_session.flush()

    with time_machine.travel(_NOW, tick=False):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="warm-alias",
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)

    assert model.last_warmup_at == _NOW
    assert model.last_used_at is None
    assert model.inactivity_alerted_at == alerted_at


@pytest.mark.integration
async def test_stamp_failure_does_not_fail_the_call(db_session: AsyncSession, app_settings: Settings) -> None:
    # Bookkeeping is best-effort: a broken stamp session must not cost the user their turn.
    await _create_model(db_session, app_settings)

    @asynccontextmanager
    async def broken() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("no engine")
        yield  # pragma: no cover — makes this an async context manager

    completion = await dispatch_chat(
        db_session,
        app_settings,
        model_alias="warm-alias",
        messages=[ChatMessage(role="user", content="hi")],
        provider=_StubProvider(),
        session_provider=broken,
    )

    assert completion.choices[0].message.content == "ok"


@pytest.mark.integration
async def test_stamps_do_not_bump_updated_at(db_session: AsyncSession, app_settings: Settings) -> None:
    # `updated_at` is documented as the last update to the row; a message or a warmup poll is a
    # system write, so it must not masquerade as an admin edit.
    model = await _create_model(db_session, app_settings)
    before = model.updated_at

    await dispatch_chat(
        db_session,
        app_settings,
        model_alias="warm-alias",
        messages=[ChatMessage(role="user", content="hi")],
        provider=_StubProvider(),
        session_provider=_session_provider(db_session),
    )
    await dispatch_probe(
        db_session,
        app_settings,
        model_alias="warm-alias",
        provider=_StubProvider(),
        session_provider=_session_provider(db_session),
    )
    await db_session.refresh(model)

    assert model.updated_at == before
    assert model.last_used_at is not None
    assert model.last_warmup_at is not None


@pytest.mark.integration
async def test_repeat_probe_does_not_rewrite_a_fresh_warmup_stamp(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Clients poll a waking endpoint every few seconds; only the newest probe carries any
    # information, so the rest must not serialise a write each on the same row.
    model = await _create_model(db_session, app_settings)

    with time_machine.travel(_NOW, tick=False):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="warm-alias",
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)
    first = model.last_warmup_at

    with time_machine.travel(_NOW + timedelta(seconds=20), tick=False):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="warm-alias",
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)

    assert model.last_warmup_at == first


@pytest.mark.integration
async def test_probe_restamps_once_the_guard_window_passes(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await _create_model(db_session, app_settings)
    later = _NOW + timedelta(minutes=30)

    with time_machine.travel(_NOW, tick=False):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="warm-alias",
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    with time_machine.travel(later, tick=False):
        await dispatch_probe(
            db_session,
            app_settings,
            model_alias="warm-alias",
            provider=_StubProvider(),
            session_provider=_session_provider(db_session),
        )
    await db_session.refresh(model)

    assert model.last_warmup_at == later
