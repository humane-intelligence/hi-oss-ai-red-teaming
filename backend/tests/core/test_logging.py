"""Tests for app.core.logging — structlog + stdlib wiring."""

import json
import logging

import pytest

from app.core.logging import configure_logging
from app.core.logging import get_logger
from tests.conftest import make_settings


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_configure_logging_sets_root_level() -> None:
    configure_logging(make_settings(log_level="WARNING"))

    assert logging.getLogger().level == logging.WARNING


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_configure_logging_installs_single_root_handler() -> None:
    configure_logging(make_settings())
    configure_logging(make_settings())

    assert len(logging.getLogger().handlers) == 1


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_configure_logging_strips_uvicorn_handlers() -> None:
    logging.getLogger("uvicorn").addHandler(logging.NullHandler())
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())

    configure_logging(make_settings())

    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        assert uv.handlers == []
        assert uv.propagate is True

    # uvicorn.access is silenced — LoggingMiddleware owns access logging.
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is False
    assert access.disabled is True


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_json_format_renders_structured_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))

    get_logger("test").info("ping", user_id=42)

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["message"] == "ping"
    assert payload["user_id"] == 42
    assert payload["level"] == "info"
    assert payload["logger"] == "test"
    assert payload["service"] == "ai-red-teaming-api"
    assert "timestamp" in payload


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_json_format_stamps_configured_service_name(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json", service_name="ai-red-teaming-worker"))

    get_logger("test").info("ping")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["service"] == "ai-red-teaming-worker"


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_console_format_renders_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="console"))

    get_logger("test").info("ping", user_id=42)

    out = capsys.readouterr().out
    assert "ping" in out
    assert "user_id" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_console_format_renders_exceptions_without_locals(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="console"))

    def _raise() -> None:
        # The source line itself (this assignment) always shows up as code context, so
        # the check value must be computed rather than spelled out anywhere nearby
        # (as a literal or in a comment) or the assertion below couldn't discriminate.
        secret_local = 12345 * 67890  # noqa: F841
        raise ValueError("boom")

    try:
        _raise()
    except ValueError:
        get_logger("test").exception("failed")

    out = capsys.readouterr().out
    assert "ValueError" in out
    assert "boom" in out
    assert "838102050" not in out


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_stdlib_logs_flow_through_structlog_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))

    logging.getLogger("third_party").warning("legacy message")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["message"] == "legacy message"
    assert payload["level"] == "warning"
    assert payload["logger"] == "third_party"
    assert payload["service"] == "ai-red-teaming-api"


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_log_level_filters_lower_severity(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_level="ERROR", log_format="json"))

    get_logger("test").info("dropped")
    get_logger("test").error("kept")

    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    events = [json.loads(line)["message"] for line in lines]
    assert "dropped" not in events
    assert "kept" in events
