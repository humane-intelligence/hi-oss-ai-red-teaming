"""Tests for app.core.sentry — opt-in Sentry init and end-to-end error reporting."""

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

from app.core import sentry as sentry_module
from app.core.error_handlers import register_error_handlers
from app.core.exceptions import BadGatewayError
from app.core.logging import configure_logging
from app.core.middleware.logging import LoggingMiddleware
from app.core.sentry import init_sentry
from tests.conftest import make_settings


@pytest.mark.unit
def test_init_sentry_is_a_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

    init_sentry(make_settings(sentry_dsn=None))

    assert calls == []


@pytest.mark.unit
def test_init_sentry_is_a_noop_with_empty_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `SENTRY_DSN=` (empty string) must disable Sentry too, not just an unset var.

    `SecretStr("")` is not `None`, so a naive `is None` check would call
    `sentry_sdk.init(dsn="")` here — harmless in practice (empty dsn ⇒ no
    transport), but it defeats the documented "unset = disabled" contract.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

    init_sentry(make_settings(sentry_dsn=SecretStr("")))

    assert calls == []


@pytest.mark.unit
def test_init_sentry_initializes_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))

    init_sentry(
        make_settings(sentry_dsn=SecretStr("https://key@sentry.example.com/1"), environment="prod", git_sha="deadbee")
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["dsn"] == "https://key@sentry.example.com/1"
    assert call["environment"] == "prod"
    assert call["release"] == "deadbee"
    assert call["send_default_pii"] is False
    assert call["max_request_body_size"] == "never"
    assert call["include_local_variables"] is False
    assert call["traces_sample_rate"] == 0.0
    assert call["trace_propagation_targets"] == []
    starlette, fastapi = call["integrations"]
    assert isinstance(starlette, StarletteIntegration)
    assert isinstance(fastapi, FastApiIntegration)
    assert starlette.failed_request_status_codes == {500}
    assert fastapi.failed_request_status_codes == {500}


@pytest.mark.unit
def test_init_sentry_swallows_a_malformed_dsn() -> None:
    """A typo'd `SENTRY_DSN` must degrade to "no error reporting", not crash the process.

    `sentry_sdk.init` raises `BadDsn` (a `ValueError`) synchronously for an unparsable
    DSN; `init_sentry` runs at import time in `main.py`, so an uncaught exception here
    would be a fatal ASGI startup failure (the app module never finishes importing).
    """
    init_sentry(make_settings(sentry_dsn=SecretStr("not-a-valid-dsn")))


class _NullTransport(Transport):
    """Swallows every envelope so the test never touches the network."""

    def capture_envelope(self, envelope: object) -> None:
        pass


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Event]]:
    """Run the real Sentry init with a `before_send` that records events and drops them.

    Recording in `before_send` (which fires after the integrations' event processors)
    yields the events that *would* be sent; the null transport keeps sessions and client
    reports off the wire too. Tears the global client down so it never leaks into other
    tests.
    """
    captured: list[Event] = []
    real_init = sentry_module.sentry_sdk.init

    def _init(**kwargs: Any) -> None:
        def _record(event: Event, _hint: Hint) -> None:
            # Returning None drops the event, so nothing is sent — we only record it.
            captured.append(event)

        real_init(**kwargs, before_send=_record, transport=_NullTransport())

    monkeypatch.setattr(sentry_module.sentry_sdk, "init", _init)
    yield captured
    client = sentry_module.sentry_sdk.get_client()
    sentry_module.sentry_sdk.get_global_scope().set_client(None)
    client.close()


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_reports_unhandled_500_with_request_id_not_502(sentry_events: list[Event]) -> None:
    """End to end: an unhandled 500 is reported once, tagged, body-free; a handled 502 is not.

    Exercises what the kwargs test can't: that the 500-only filter actually attaches (it
    only does when init runs before the middleware stack is built) and that
    `LoggingMiddleware` puts `request_id` on the captured event.
    """
    # Configure logging so the catch-all's logger.exception emits a record the
    # LoggingIntegration would turn into a second event — the duplicate `ignore_logger`
    # suppresses. Without this the "== 1" assertion below would pass trivially.
    configure_logging(make_settings(log_format="json"))
    init_sentry(make_settings(sentry_dsn=SecretStr("http://public@localhost/1")))

    app = FastAPI()
    app.add_middleware(LoggingMiddleware)
    register_error_handlers(app)

    @app.get("/bad")
    def _bad() -> None:
        raise BadGatewayError("flaky target model")

    @app.post("/boom")
    def _boom() -> None:
        raise RuntimeError("unhandled")

    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/bad").status_code == 502
    assert sentry_events == []

    response = client.post("/boom", json={"prompt": "red-team payload"})
    assert response.status_code == 500
    # Exactly one: the catch-all's re-log is ignored (see init_sentry), so the Starlette
    # capture is the only event — not double-counted.
    assert len(sentry_events) == 1
    event = sentry_events[0]
    # request_id comes from LoggingMiddleware's set_tag (a 32-char uuid4 hex), which
    # survives the contextvars unwind that would leave a before_send tagger empty here.
    assert re.fullmatch(r"[0-9a-f]{32}", event["tags"]["request_id"])
    # max_request_body_size="never" keeps the request body out of the event.
    assert "red-team payload" not in json.dumps(event, default=str)


@pytest.mark.unit
@pytest.mark.usefixtures("_reset_logging")
def test_local_variables_are_not_captured(sentry_events: list[Event]) -> None:
    """`include_local_variables=False` — neither `send_default_pii` nor
    `max_request_body_size` (checked above) touches per-frame local variables, so a
    secret merely sitting in scope on the exception's stack (e.g. a DB password held
    locally by asyncpg's connect internals) must be kept out some other way.
    """
    configure_logging(make_settings(log_format="json"))
    init_sentry(make_settings(sentry_dsn=SecretStr("http://public@localhost/1")))

    app = FastAPI()
    app.add_middleware(LoggingMiddleware)
    register_error_handlers(app)

    @app.post("/boom")
    def _boom() -> None:
        # Computed, not spelled out here: Sentry quotes this frame's own source lines
        # as context regardless of include_local_variables, so a literal value would
        # leak via source text and the check below couldn't discriminate.
        db_password = 12345 * 67890  # noqa: F841
        raise RuntimeError("unhandled")

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/boom")
    assert response.status_code == 500
    assert len(sentry_events) == 1
    assert "838102050" not in json.dumps(sentry_events[0], default=str)
