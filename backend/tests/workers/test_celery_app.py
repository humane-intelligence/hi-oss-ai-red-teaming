"""Tests for app.workers.celery_app — worker wiring (logging + Sentry signals)."""

import json

import pytest
from celery.signals import celeryd_init
from celery.signals import setup_logging

from app.core.logging import get_logger
from app.workers import celery_app
from tests.conftest import make_settings


@pytest.mark.unit
def test_setup_logging_signal_runs_our_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(celery_app, "configure_logging", calls.append)

    setup_logging.send(sender=None)

    assert len(calls) == 1


@pytest.mark.unit
def test_celeryd_init_signal_runs_our_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(celery_app, "init_sentry", calls.append)

    celeryd_init.send(sender="worker1@host")

    assert calls == [celery_app._settings]


@pytest.mark.unit
def test_task_lifecycle_events_are_enabled() -> None:
    assert celery_app.app.conf.worker_send_task_events is True
    assert celery_app.app.conf.task_send_sent_event is True


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_worker_logging_runs_structlog_json_pipeline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(celery_app, "get_settings", lambda: make_settings(service_name="ai-red-teaming-worker"))

    celery_app._configure_worker_logging()
    get_logger("worker-test").info("task ping")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["message"] == "task ping"
    assert payload["service"] == "ai-red-teaming-worker"
