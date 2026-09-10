"""Tests for app.core.email.backends.smtp.SMTPBackend — mocked smtplib."""

import smtplib
import ssl
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.core.email.backends import smtp as smtp_module
from app.core.email.backends.base import EmailMessage
from app.core.email.backends.base import InlineImage
from app.core.email.backends.base import TransientEmailError
from app.core.email.backends.smtp import SMTPBackend


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "smtp_host": "mail.example.com",
        "smtp_port": 587,
        "smtp_username": None,
        "smtp_password": None,
        "smtp_tls": "none",
    }
    return SimpleNamespace(**{**base, **overrides})


def _server() -> MagicMock:
    server = MagicMock()
    server.__enter__.return_value = server
    return server


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
    *,
    server: MagicMock | None = None,
    ssl_server: MagicMock | None = None,
) -> None:
    monkeypatch.setattr(smtp_module, "get_settings", lambda: settings)
    if server is not None:
        monkeypatch.setattr(smtp_module.smtplib, "SMTP", lambda *_a, **_k: server)
    if ssl_server is not None:
        monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", lambda *_a, **_k: ssl_server)


def _parts(mime: Any) -> dict[str, str]:
    return {part.get_content_type(): part.get_content().strip() for part in mime.iter_parts()}


@pytest.mark.unit
def test_smtp_backend_sends_multipart_via_starttls(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    _patch(monkeypatch, _settings(smtp_tls="starttls"), server=server)

    result = SMTPBackend().send(message)

    assert result is None
    server.starttls.assert_called_once()
    server.send_message.assert_called_once()
    mime = server.send_message.call_args.args[0]
    assert mime["From"] == "noreply@example.com"
    assert mime["To"] == "rcpt@example.com"
    assert mime["Subject"] == "Hi"
    assert mime.get_content_type() == "multipart/alternative"
    assert _parts(mime) == {"text/plain": "Hello.", "text/html": "<p>Hello.</p>"}


@pytest.mark.unit
def test_smtp_backend_embeds_inline_image_as_related(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    _patch(monkeypatch, _settings(), server=server)
    with_logo = message.model_copy(update={"inline_images": [InlineImage(cid="logo", data=b"\x89PNG\r\n\x1a\nfake")]})

    SMTPBackend().send(with_logo)

    mime = server.send_message.call_args.args[0]
    images = [part for part in mime.walk() if part.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<logo>"
    assert images[0].get_content_type() == "image/png"


@pytest.mark.unit
def test_smtp_backend_plain_skips_starttls(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    _patch(monkeypatch, _settings(smtp_tls="none"), server=server)

    SMTPBackend().send(message)

    server.starttls.assert_not_called()
    server.send_message.assert_called_once()


@pytest.mark.unit
def test_smtp_backend_ssl_uses_smtp_ssl(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    _patch(monkeypatch, _settings(smtp_tls="ssl"), ssl_server=server)

    SMTPBackend().send(message)

    server.send_message.assert_called_once()
    server.starttls.assert_not_called()


@pytest.mark.unit
def test_smtp_backend_starttls_verifies_server_cert(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    """STARTTLS must authenticate the server — smtplib's default context does not."""
    server = _server()
    _patch(monkeypatch, _settings(smtp_tls="starttls"), server=server)

    SMTPBackend().send(message)

    context = server.starttls.call_args.kwargs["context"]
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED


@pytest.mark.unit
def test_smtp_backend_ssl_verifies_server_cert(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    """Implicit TLS (SMTP_SSL) must pass a verifying context, not the unverified default."""
    captured: dict[str, Any] = {}
    server = _server()

    def _capture(*_a: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return server

    _patch(monkeypatch, _settings(smtp_tls="ssl"))
    monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", _capture)

    SMTPBackend().send(message)

    context = captured["context"]
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED


@pytest.mark.unit
def test_smtp_backend_logs_in_when_credentials_set(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    _patch(monkeypatch, _settings(smtp_username="user", smtp_password=SecretStr("secret")), server=server)

    SMTPBackend().send(message)

    server.login.assert_called_once_with("user", "secret")


@pytest.mark.unit
def test_smtp_backend_disconnect_is_transient(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    server.send_message.side_effect = smtplib.SMTPServerDisconnected("bye")
    _patch(monkeypatch, _settings(), server=server)

    with pytest.raises(TransientEmailError):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_4xx_reply_is_transient(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    server.send_message.side_effect = smtplib.SMTPDataError(421, b"service unavailable")
    _patch(monkeypatch, _settings(), server=server)

    with pytest.raises(TransientEmailError):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_5xx_reply_reraises(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    server.send_message.side_effect = smtplib.SMTPSenderRefused(550, b"bad sender", "noreply@example.com")
    _patch(monkeypatch, _settings(), server=server)

    with pytest.raises(smtplib.SMTPSenderRefused):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_recipients_refused_reraises(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    server = _server()
    server.send_message.side_effect = smtplib.SMTPRecipientsRefused({"rcpt@example.com": (550, b"no such user")})
    _patch(monkeypatch, _settings(), server=server)

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_sets_connect_timeout(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    captured: dict[str, Any] = {}
    server = _server()

    def _capture(*_a: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return server

    _patch(monkeypatch, _settings())
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _capture)

    SMTPBackend().send(message)

    assert captured.get("timeout") is not None


@pytest.mark.unit
def test_smtp_backend_connect_timeout_is_transient(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    def _timeout(*_a: Any, **_k: Any) -> smtplib.SMTP:
        raise TimeoutError("timed out")

    _patch(monkeypatch, _settings())
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _timeout)

    with pytest.raises(TransientEmailError):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_connection_refused_is_transient(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    def _refuse(*_a: Any, **_k: Any) -> smtplib.SMTP:
        raise ConnectionRefusedError("refused")

    _patch(monkeypatch, _settings())
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _refuse)

    with pytest.raises(TransientEmailError):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_tls_error_reraises(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    """A cert/handshake failure is permanent — it falls through the transient map and propagates."""
    server = _server()
    server.starttls.side_effect = ssl.SSLError("certificate verify failed")
    _patch(monkeypatch, _settings(smtp_tls="starttls"), server=server)

    with pytest.raises(ssl.SSLError):
        SMTPBackend().send(message)


@pytest.mark.unit
def test_smtp_backend_missing_host_raises(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    _patch(monkeypatch, _settings(smtp_host=None))

    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        SMTPBackend().send(message)
