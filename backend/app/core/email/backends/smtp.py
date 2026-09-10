"""SMTPBackend — transactional email through a configured SMTP server (stdlib)."""

import smtplib
import socket
import ssl

from app.core.config import get_settings
from app.core.email.backends.base import EmailMessage
from app.core.email.backends.base import TransientEmailError
from app.core.email.backends.mime import build_mime

_SMTP_TIMEOUT = 30


class SMTPBackend:
    """Send mail through a configured SMTP server.

    SMTP exposes no provider message id, so `send` returns None. Transient
    failures (disconnect, 4xx reply, connection/DNS/timeout) raise
    `TransientEmailError`; permanent ones (5xx, refused recipients, auth,
    TLS/cert) propagate unchanged — a bad cert won't fix itself on retry.
    """

    def send(self, message: EmailMessage) -> None:
        settings = get_settings()
        host = settings.smtp_host
        if not host:
            msg = "SMTP backend selected but SMTP_HOST is not set"
            raise RuntimeError(msg)

        # Verify the server cert + hostname on any TLS path. smtplib's default
        # (no context) is ssl._create_stdlib_context() — CERT_NONE, no hostname
        # check — so "encrypted" SMTP would still be MITM-able.
        context = ssl.create_default_context() if settings.smtp_tls != "none" else None

        mime = build_mime(message)
        try:
            # STARTTLS / login stay inside the `with` so a failure there still
            # closes the socket (a connected SMTP object isn't context-managed
            # until the `with` binds).
            with _open(host, settings.smtp_port, use_ssl=settings.smtp_tls == "ssl", context=context) as server:
                if settings.smtp_tls == "starttls":
                    server.starttls(context=context)
                if settings.smtp_username:
                    password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
                    server.login(settings.smtp_username, password)
                server.send_message(mime)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            raise TransientEmailError(str(exc)) from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx is a temporary failure; 5xx (and refused sender/auth) are permanent.
            if str(exc.smtp_code).startswith("4"):
                raise TransientEmailError(str(exc)) from exc
            raise
        except (ConnectionError, TimeoutError, socket.gaierror) as exc:
            # Socket-level failure (refused, reset, DNS, timeout) — retryable.
            # Note: smtplib.SMTPException and ssl.SSLError both subclass OSError,
            # so this must stay narrow — a broad `except OSError` would swallow
            # permanent failures like SMTPRecipientsRefused or a cert error,
            # which by design fall through here and propagate (no retry).
            raise TransientEmailError(str(exc)) from exc


def _open(host: str, port: int, *, use_ssl: bool, context: ssl.SSLContext | None) -> smtplib.SMTP:
    # Finite timeout is mandatory: without it smtplib inherits the global default
    # (None in this process), so a dead/slow server hangs the Celery worker forever
    # instead of failing into a retry. socket.timeout is TimeoutError → mapped transient.
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT, context=context)
    return smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
