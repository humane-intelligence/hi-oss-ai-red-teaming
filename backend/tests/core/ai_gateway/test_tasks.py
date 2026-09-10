from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.core.ai_gateway.tasks as tasks_mod
from app.core.ai_gateway.tasks import check_model_inactivity
from app.core.ai_gateway.tasks import run_model_health_check
from app.core.config import get_settings
from app.workers.celery_app import app as celery_app


@pytest.mark.unit
def test_task_invokes_run_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    async def fake_run(session: Any, settings: Any, model_id: Any, *, provider: Any = None) -> None:
        called["model_id"] = model_id

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield AsyncMock()

    monkeypatch.setattr(tasks_mod, "run_health_check", fake_run)
    monkeypatch.setattr(tasks_mod, "async_session_scope", fake_scope)

    run_model_health_check("11111111-1111-1111-1111-111111111111")

    assert str(called["model_id"]) == "11111111-1111-1111-1111-111111111111"


@pytest.mark.unit
def test_task_has_time_limit_backstop() -> None:
    # A hard time_limit bounds the probe loop so a wedged check can't run forever (self-heal has no reaper).
    assert run_model_health_check.time_limit == get_settings().health_check_task_time_limit_seconds


@pytest.mark.unit
def test_inactivity_task_invokes_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()

    async def fake_sweep(passed_session: Any) -> int:
        assert passed_session is session
        return 3

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield session

    monkeypatch.setattr(tasks_mod, "alert_inactive_models", fake_sweep)
    monkeypatch.setattr(tasks_mod, "async_session_scope", fake_scope)

    assert check_model_inactivity() == {"alerted": 3}
    # The sweep commits per alert (a mail queued against an uncommitted row is dropped),
    # so the task must not hold a commit of its own back to the end of the run.
    session.commit.assert_not_awaited()


@pytest.mark.unit
def test_inactivity_task_is_discovered_by_celery_app() -> None:
    assert "app.core.ai_gateway.tasks.check_model_inactivity" in celery_app.tasks


@pytest.mark.unit
def test_inactivity_task_is_scheduled_on_beat() -> None:
    entry = celery_app.conf.beat_schedule["check-model-inactivity"]

    assert entry["task"] == "app.core.ai_gateway.tasks.check_model_inactivity"
    assert entry["schedule"] == float(get_settings().model_inactivity_check_interval_seconds)
