"""Tests for app.core.email.backends — message model + ConsoleBackend + factory."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.email import backends as backends_module
from app.core.email.backends import ConsoleBackend
from app.core.email.backends import EmailMessage
from app.core.email.backends import SESBackend
from app.core.email.backends import SMTPBackend
from app.core.email.backends import console as console_module
from app.core.email.backends import get_email_backend


@pytest.mark.unit
def test_email_message_rejects_invalid_address() -> None:
    with pytest.raises(ValueError, match="value is not a valid email"):
        EmailMessage(
            to="not-an-email",
            subject="x",
            body_text="x",
            body_html="x",
            from_addr="noreply@example.com",
        )


@pytest.mark.unit
@pytest.mark.parametrize("subject", ["hi\r\nBcc: a@b.com", "hi\nx", "hi\rx"])
def test_email_message_rejects_crlf_in_subject(subject: str) -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        EmailMessage(
            to="rcpt@example.com",
            subject=subject,
            body_text="x",
            body_html="x",
            from_addr="noreply@example.com",
        )


@pytest.mark.unit
def test_console_backend_logs_event(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    calls: list[dict[str, Any]] = []

    class _Capturer:
        @staticmethod
        def info(event: str, **kwargs: Any) -> None:
            calls.append({"event": event, **kwargs})

    monkeypatch.setattr(console_module, "logger", _Capturer())

    ConsoleBackend().send(message)

    assert len(calls) == 1
    assert calls[0]["event"] == "email.sent.console"
    assert calls[0]["to"] == "rcpt@example.com"
    assert calls[0]["subject"] == "Hi"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("backend_name", "expected"),
    [("console", ConsoleBackend), ("ses", SESBackend), ("smtp", SMTPBackend)],
)
def test_get_email_backend_builds_configured_backend(
    monkeypatch: pytest.MonkeyPatch, backend_name: str, expected: type
) -> None:
    monkeypatch.setattr(backends_module, "get_settings", lambda: SimpleNamespace(email_backend=backend_name))
    assert isinstance(get_email_backend(), expected)
