"""Request/response models for data licenses."""

from datetime import datetime
from typing import Any
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.catalog import curated_ships_content
from app.core.licenses.models import DataLicense


def _require_http_url(value: str | None) -> str | None:
    """Reject a `reference_url` whose scheme isn't http(s).

    The value is surfaced verbatim as an anchor `href` in the console, so a `javascript:` / `data:`
    URL would be a stored-XSS-on-click vector. A scheme allowlist at the write edge is the authority
    (the render-side guard is defense-in-depth). `None` passes — it clears/omits the field.
    """
    if value is None:
        return value
    if urlsplit(value).scheme.lower() not in ("http", "https"):
        raise ValueError("must be an http(s) URL")
    return value


def _require_text(value: str | None) -> str | None:
    """Reject a legal text that is only whitespace.

    `min_length` counts characters, so `"   "` passes it — and a blank body still flips the stored
    `has_text` flag, which is what makes a licence render as a readable document. The reader would
    then open an empty dialog. `None` passes; the explicit-null guard handles it.
    """
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value


class DataLicenseSummary(BaseModel):
    """A data licence as it appears in the list / picker — no full body."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "spdx_id": "CC-BY-4.0",
                "name": "Creative Commons Attribution 4.0 International",
                "version": "4.0",
                "short_description": "Share and adapt for any purpose, with attribution.",
                "reference_url": "https://creativecommons.org/licenses/by/4.0/legalcode",
                "is_curated": True,
                "is_default": True,
                "has_content": False,
                "is_no_license": False,
                "text_managed_in_code": False,
                "protects_conversation_data": False,
                "created_by_id": None,
            }
        }
    )

    id: UUID = Field(description="Stable id — the value stored on evaluations/groups and the platform default.")
    spdx_id: str | None = Field(
        description="SPDX id where the licence publishes one; `null` for user-authored licences and `No license`."
    )
    name: str = Field(description="Human-readable licence name / title.")
    version: str | None = Field(description="Licence version, e.g. `4.0`.")
    short_description: str = Field(description="One-line gist of the licence terms.")
    reference_url: str | None = Field(description="Canonical URL of the full licence text, if any.")
    is_curated: bool = Field(description="A platform-shipped licence (not editable by an owner).")
    is_default: bool = Field(default=False, description="The current platform default data licence.")
    has_content: bool = Field(
        description="Whether this licence carries its full text on the platform (fetch it via `GET /licenses/{id}`).",
    )
    is_no_license: bool = Field(
        description=(
            "The catalog's `No license` entry — data under it carries no licence at all, and a group "
            "pointing at it blocks the inheritance its evaluations and conversations would otherwise "
            "follow. Answered by the server so a client never has to infer the row."
        ),
    )
    text_managed_in_code: bool = Field(
        description=(
            "The platform ships this licence's text in code, so `PATCH /licenses/{id}` refuses to "
            "change it (403) and a resync would revert it anyway. `false` for every user-authored "
            "licence and for a curated one the catalog ships no text for — the one row kind whose "
            "`content` a `licenses:manage` holder may fill in."
        ),
    )
    protects_conversation_data: bool = Field(
        description=(
            "The licence forbids redistributing raw conversations: message text written under it is stored "
            "encrypted at rest — conversation titles, tags and attached images are not. A conversation takes "
            "that decision once, when it is created, so turning the flag on later covers conversations started "
            "from then on and nothing already running, including messages added to those later. Curated "
            "licences carry the catalog's value and reject a change here."
        ),
    )
    created_by_id: UUID | None = Field(description="Author of a user-created licence; `null` for curated.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null on a live row.",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the licence; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_model(cls, lic: DataLicense, *, is_default: bool = False) -> Self:
        """Project a licence row.

        `is_no_license` names the sentinel row by the id the catalog derives, so the console does not
        have to recognise it from the absence of an `spdx_id` — a shape a second catalog entry, or a
        user-authored row whose author was hard-deleted, would also take. `text_managed_in_code`
        carries the predicate the edit gate itself uses, so the console offers the text editor on
        exactly the rows the API accepts one for, rather than on a proxy that holds only while the
        sentinel is the single entry shipping a text.

        `has_content` comes from the mapped SQL expression `DataLicense.has_text`, not from the text
        itself, so every path that projects a licence can defer the column — and no call site can
        answer the question wrongly by forgetting an argument. A row the expression was never loaded
        for (one built in memory, or one whose flag is expired) raises rather than guessing.
        """
        return cls(
            id=lic.id,
            spdx_id=lic.spdx_id,
            name=lic.name,
            version=lic.version,
            short_description=lic.short_description,
            reference_url=lic.reference_url,
            is_curated=lic.created_by_id is None,
            is_default=is_default,
            has_content=lic.has_text,
            is_no_license=lic.id == curated_license_id(NO_LICENSE_SPDX_ID),
            text_managed_in_code=curated_ships_content(lic.id),
            protects_conversation_data=lic.protects_conversation_data,
            created_by_id=lic.created_by_id,
            deleted_at=lic.deleted_at,
            deleted_by_id=lic.deleted_by_id,
        )


class DataLicenseResponse(DataLicenseSummary):
    """A data licence with its full legal text — the detail view."""

    content: str = Field(description="Full legal text of the licence.")

    @classmethod
    def from_model(cls, lic: DataLicense, *, is_default: bool = False) -> Self:
        summary = DataLicenseSummary.from_model(lic, is_default=is_default)
        return cls(**summary.model_dump(), content=lic.content)


class LicenseCreate(BaseModel):
    """Create a user-authored data licence."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Acme Internal Data License 1.0",
                "version": "1.0",
                "short_description": "Internal use within Acme only; no redistribution.",
                "content": "ACME INTERNAL DATA LICENSE\n\n1. ...",
                "reference_url": "https://acme.example/licenses/internal-1.0",
            }
        }
    )

    name: str = Field(min_length=1, max_length=255, description="Human-readable licence name / title.")
    version: str | None = Field(default=None, max_length=64, description="Licence version, e.g. `1.0`.")
    short_description: str = Field(min_length=1, description="One-line gist shown in the picker.")
    content: str = Field(min_length=1, description="Full legal text of the licence. Must not be blank.")
    protects_conversation_data: bool = Field(
        default=False,
        description=(
            "Mark a licence that forbids redistributing raw conversations: message text written under it is "
            "stored encrypted at rest — titles, tags and attached images are not. A conversation takes that "
            "decision when it is created, so this covers conversations started from then on and nothing "
            "already running, including messages added to those later."
        ),
    )
    reference_url: str | None = Field(
        default=None, max_length=1024, description="Canonical URL of the full text. Must be an http(s) URL."
    )

    _validate_reference_url = field_validator("reference_url")(_require_http_url)
    _validate_content = field_validator("content")(_require_text)


class LicenseUpdate(BaseModel):
    """Patch a data licence. Omit a field to leave it unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255, description="Omit to leave unchanged.")
    version: str | None = Field(default=None, max_length=64, description="Omit to leave unchanged; `null` clears it.")
    short_description: str | None = Field(default=None, min_length=1, description="Omit to leave unchanged.")
    content: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Full legal text; must not be blank. Omit to leave unchanged. The only field editable on a curated licence."
        ),
    )
    reference_url: str | None = Field(
        default=None,
        max_length=1024,
        description="Omit to leave unchanged; `null` clears it. Must be an http(s) URL.",
    )
    protects_conversation_data: bool | None = Field(
        default=None, description="Omit to leave unchanged. Refused on a curated licence."
    )

    _validate_reference_url = field_validator("reference_url")(_require_http_url)
    _validate_content = field_validator("content")(_require_text)

    @field_validator("name", "short_description", "content", "protects_conversation_data", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # These back NOT NULL columns; omitting leaves them unchanged, but an explicit `null` is
        # meaningless. `version`/`reference_url` are nullable, so `null` legitimately clears them.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value
