"""Request/response models for Terms-of-Service documents."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.terms.models import TermsDocument

# A legal document, not a corpus: generous next to any real terms of service, and a bound is
# needed because the current version is served unauthenticated and nothing throttles that.
MAX_TERMS_CONTENT_CHARS = 200_000


def _require_text(value: str) -> str:
    """Reject a body that is only whitespace.

    `min_length` counts spaces, and a blank document would gate every user behind an empty
    page they cannot read.
    """
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _clean_version(value: str) -> str:
    """Strip a version handle and reject a blank one.

    Unstripped, `"1.0 "` and `"1.0"` are two different rows to the partial-unique index while
    reading as one version to a human — and the handle is what a consent record is answered by.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


class TermsDocumentSummary(BaseModel):
    """A published version as the admin history lists it — no body."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "version": "1.0",
                "published_at": "2026-09-02T12:00:00Z",
            }
        }
    )

    id: UUID = Field(description="Identifier a consent record points at.")
    version: str = Field(description="Version handle, unique among live documents.", examples=["1.0"])
    published_at: datetime = Field(description="When this version was published (UTC).")

    @classmethod
    def from_model(cls, document: TermsDocument) -> Self:
        return cls(id=document.id, version=document.version, published_at=document.published_at)


class TermsDocumentResponse(TermsDocumentSummary):
    """A published version with its full text — what a reader is asked to accept."""

    content: str = Field(description="Full text of the terms, as Markdown.")

    @classmethod
    def from_model(cls, document: TermsDocument) -> Self:
        summary = TermsDocumentSummary.from_model(document)
        return cls(**summary.model_dump(), content=document.content)


class TermsPublish(BaseModel):
    """Publish a new version, which becomes the one every account must accept."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "version": "1.0",
                "content": "# Terms of Service\n\n1. ...",
            }
        },
    )

    version: str = Field(
        min_length=1,
        max_length=64,
        description="Version handle shown to users and recorded on their consent, e.g. `1.0`. Trimmed.",
    )
    content: str = Field(
        min_length=1,
        max_length=MAX_TERMS_CONTENT_CHARS,
        description="Full text of the terms, as Markdown. Must not be blank.",
    )

    _validate_content = field_validator("content")(_require_text)
    _validate_version = field_validator("version")(_clean_version)


class TermsAccept(BaseModel):
    """Accept a specific version — the id the client actually rendered."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"terms_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}},
    )

    terms_id: UUID = Field(
        description=(
            "The version being accepted. Must be the current one — a publish that lands between "
            "render and submit is refused with 409 rather than accepted on the user's behalf."
        )
    )
