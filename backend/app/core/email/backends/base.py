"""Shared types for email backends: message envelope, error class, protocol.

Lives separately from concrete backends so adding a new one (SES, SendGrid)
means one new file under `backends/` and one line in `__init__.get_email_backend`
— nothing in the existing backends moves.
"""

from typing import Protocol

from pydantic import BaseModel
from pydantic import EmailStr
from pydantic import Field
from pydantic import field_validator


class InlineImage(BaseModel):
    """An image embedded in the HTML body via a `cid:` reference (multipart/related).

    `cid` is the Content-ID the HTML links to as `cid:<cid>`; stored without
    angle brackets — the MIME builder adds them when writing the header.
    """

    cid: str
    data: bytes
    subtype: str = "png"


class EmailMessage(BaseModel):
    """One transactional email ready to hand to a backend.

    `body_html` and `body_text` are both required — every backend ships
    multipart/alternative so clients render the right part. `inline_images`
    (e.g. the brand logo) ride along as `multipart/related` parts referenced
    from the HTML by `cid:`.
    """

    to: EmailStr
    subject: str
    body_text: str
    body_html: str
    from_addr: EmailStr
    inline_images: list[InlineImage] = Field(default_factory=list)

    @field_validator("subject")
    @classmethod
    def _reject_crlf(cls, v: str) -> str:
        """Block CR/LF in subject — RFC 5322 subjects are single-line.

        Bare CR/LF would let user-controlled context inject extra headers
        (Bcc, Reply-To, …) in any backend that doesn't sanitise on its own.
        """
        if "\r" in v or "\n" in v:
            msg = "subject must not contain CR or LF (header injection risk)"
            raise ValueError(msg)
        return v


class TransientEmailError(Exception):
    """Backend-level retryable failure (connect blip, 4xx SMTP, rate limit).

    Permanent errors (5xx, malformed address, auth failure) must NOT use this
    — they should raise their native exception so the Celery task fails fast
    instead of burning the retry budget.
    """


class EmailBackend(Protocol):
    """Wire-level email sender. Implementations are stateless / cheap to build."""

    def send(self, message: EmailMessage) -> str | None:
        """Deliver `message`; return the provider-side message id when one exists.

        Raise `TransientEmailError` on retryable failures. Backends without a
        provider message id (console, SMTP) return None.
        """
        ...
