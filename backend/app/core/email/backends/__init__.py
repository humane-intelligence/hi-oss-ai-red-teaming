"""Email backends — shared protocol + concrete adapters + factory.

Public surface is re-exported here so callers stay on
`from app.core.email.backends import X` regardless of which submodule
the symbol actually lives in. Adding a new backend = one new file in
this package + one branch in `get_email_backend`.
"""

from app.core.config import get_settings
from app.core.email.backends.base import EmailBackend
from app.core.email.backends.base import EmailMessage
from app.core.email.backends.base import InlineImage
from app.core.email.backends.base import TransientEmailError
from app.core.email.backends.console import ConsoleBackend
from app.core.email.backends.ses import SESBackend
from app.core.email.backends.smtp import SMTPBackend

__all__ = [
    "ConsoleBackend",
    "EmailBackend",
    "EmailMessage",
    "InlineImage",
    "SESBackend",
    "SMTPBackend",
    "TransientEmailError",
    "get_email_backend",
]


def get_email_backend() -> EmailBackend:
    """Build the backend named by `Settings.email_backend` (fresh each call)."""
    settings = get_settings()
    if settings.email_backend == "console":
        return ConsoleBackend()
    if settings.email_backend == "ses":
        return SESBackend()
    if settings.email_backend == "smtp":
        return SMTPBackend()
    msg = f"Email backend not implemented: {settings.email_backend}"
    raise NotImplementedError(msg)
