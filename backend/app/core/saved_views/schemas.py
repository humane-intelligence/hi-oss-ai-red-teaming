"""Request/response schemas for the saved-views endpoints.

`SavedViewResponse` is the public projection; `SavedViewCreate` /
`SavedViewUpdate` back the write paths. `SavedViewUpdateChanges` is the
service-owned, HTTP-agnostic update contract — building it from
`payload.model_dump(exclude_unset=True)` lets `model_fields_set` separate an
omitted field from an explicit value.

`state` is a standardized envelope (`SavedViewState`): the backend fixes and
validates the shape, but `filters` values and column ids stay opaque — which
filters/columns/sort keys are valid for a given list is the frontend's concern.
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.saved_views.enums import SavedViewResource

if TYPE_CHECKING:
    from app.core.saved_views.models import SavedView

# `filters` values are client-supplied; cap the serialized envelope size so an
# authenticated caller can't persist multi-MB / deeply-nested blobs on their rows.
_MAX_STATE_BYTES = 64 * 1024


def _assert_state_size(state: dict[str, Any]) -> None:
    if len(json.dumps(state).encode()) > _MAX_STATE_BYTES:
        raise ValueError(f"state exceeds the {_MAX_STATE_BYTES // 1024} KiB limit")


class SavedViewState(BaseModel):
    """Standardized, list-agnostic shape of a saved view's state.

    Mirrors the platform list query-param vocabulary (`order_by`, `limit`/`offset`,
    `search`). Per-list specifics stay opaque — `filters` values and column ids are
    the frontend's to define, so the envelope is fixed but the backend never checks
    a value is valid for a given list (that would be per-list coupling). `extra=
    "forbid"` rejects unknown top-level keys; adding a new state category is one
    field here (no migration — the column is JSONB), while removing, renaming, or
    tightening a field (`extra="forbid"` re-validates on read) breaks reads of
    rows that carry the old shape — migrate those rows first.
    """

    model_config = ConfigDict(extra="forbid")

    order_by: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^-?[a-zA-Z_][a-zA-Z0-9_]*$",
        description="Sort token; optional leading `-` selects descending.",
    )
    filters: dict[str, Any] = Field(default_factory=dict, description="Per-list filter values (opaque to the backend).")
    hidden_columns: list[str] = Field(default_factory=list, description="Ids of columns hidden in table views.")
    search: str | None = Field(default=None, max_length=255, description="Free-text search term.")
    limit: int | None = Field(default=None, ge=1, le=100, description="Page size (mirrors list pagination).")
    offset: int | None = Field(default=None, ge=0, description="Zero-based page offset.")


_EXAMPLE_STATE: dict[str, Any] = {
    "order_by": "-created_at",
    "filters": {"status": "published"},
    "hidden_columns": ["created_at"],
}


class SavedViewResponse(BaseModel):
    """Public view of a `SavedView` row.

    Build via `SavedViewResponse.from_model(view)` so the projection stays in
    one place.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                "resource": "evaluations",
                "name": "My published, newest first",
                "state": _EXAMPLE_STATE,
                "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
                "created_at": "2026-01-01T12:00:00Z",
                "updated_at": "2026-01-02T09:30:00Z",
            }
        }
    )

    id: UUID = Field(description="Server-assigned identifier.")
    resource: SavedViewResource = Field(description="List resource the view is scoped to.")
    name: str = Field(description="User-given name of the view.")
    state: SavedViewState = Field(description="Standardized view state (filters, sort, columns).")
    created_by_id: UUID = Field(description="User who owns the view.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    # No `deleted_by_id` here, unlike the other restorable responses: views are
    # owner-scoped with no break-glass, so the deleter is always the caller reading it.
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )

    @classmethod
    def from_model(cls, view: SavedView) -> SavedViewResponse:
        """Project a `SavedView` ORM row into the public response shape."""
        return cls(
            id=view.id,
            resource=SavedViewResource(view.resource),
            name=view.name,
            state=SavedViewState.model_validate(view.state),
            created_by_id=view.created_by_id,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
        )


class SavedViewCreate(BaseModel):
    """Payload accepted by `POST /v1/saved-views`.

    The owner is the authenticated caller. `state` is normalized to the full
    `SavedViewState` envelope (omitted sub-fields take their defaults); omit it
    to save the defaults. A `(resource, name)` already used by the caller is a
    409 conflict.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "resource": "evaluations",
                "name": "My published, newest first",
                "state": _EXAMPLE_STATE,
            }
        }
    )

    resource: SavedViewResource = Field(description="List resource the view applies to.")
    name: str = Field(min_length=1, max_length=128, description="Name for the view (unique per resource per user).")
    state: SavedViewState = Field(default_factory=SavedViewState, description="Standardized view state.")

    @field_validator("state")
    @classmethod
    def _cap_state_size(cls, value: SavedViewState) -> SavedViewState:
        _assert_state_size(value.model_dump())
        return value


class SavedViewUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/saved-views/{id}`.

    `name` and `state` are editable; `resource` is immutable. Omitted fields
    stay unchanged; `state` is replaced wholesale when given. Explicit `null` is
    rejected for both (they back NOT NULL columns).
    """

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Renamed view"}})

    name: str | None = Field(default=None, min_length=1, max_length=128, description="Omit to leave unchanged.")
    state: SavedViewState | None = Field(
        default=None, description="Omit to leave unchanged; replaces the state wholesale when set."
    )

    @field_validator("name", "state", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Both back NOT NULL columns; omitting leaves the row untouched, but an
        # explicit `null` would crash the flush. Reject at the edge with a 422.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value

    @field_validator("state")
    @classmethod
    def _cap_state_size(cls, value: SavedViewState | None) -> SavedViewState | None:
        if value is not None:
            _assert_state_size(value.model_dump())
        return value


class SavedViewUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `SavedViewUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from a supplied value.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    state: dict[str, Any] | None = None
