"""Row filters for an export — the create-request surface and the worker's replay carrier.

Kept in its own module (not `schemas.py`) so both `schemas.ExportJobCreate` and `base.ExportScope`
can depend on it without an import cycle (`schemas` imports from `base`). Every field is `None` =
no constraint; applicability is per template (see `ExportFilters`), and an inapplicable filter is
ignored, never an error.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.annotations.enums import FlagStatus
from app.core.helpers import ensure_utc
from app.core.reviews.enums import ReviewStatus

# Every status a template's `status` filter may name — the union of the domain status enums (all
# currently share the same members). An unknown value is rejected at request time (see `ExportFilters`).
_KNOWN_STATUSES = {s.value for s in FlagStatus} | {s.value for s in ReviewStatus}


def coerce_status[E: StrEnum](enum_cls: type[E], value: str | None) -> E | None:
    """Map the free-text export `status` filter onto a template's own status enum.

    Returns `None` (no constraint) when `value` is unset. An unknown status is rejected upstream at
    `ExportFilters` validation (→ 422), so reaching here with a non-member is not expected; the
    `except` is a defensive fallback keeping a template with a divergent status vocabulary ignoring
    an inapplicable value rather than failing the (detached) worker.
    """
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


class ExportFilters(BaseModel):
    """Optional export row filters, each honored only by the templates where the dimension exists.

    Per-template applicability lives on each template's `CsvExport.supported_filters`: `task_id`
    narrows only the flags export; `status` only flags/reviews; `scenario_id` flags/conversations/
    conversation-groups/transcript/engagement_report; `created_from`/`created_to` and `user_id`
    apply broadly. A dimension the chosen template doesn't support is **rejected at request time
    (400)**, not silently dropped. `evaluation` is the export scope, not a filter here.
    """

    # Reject unknown keys (422) rather than silently ignoring a typo'd filter and running the
    # export unfiltered on that dimension — the typed FE can't hit it, but a raw API client can.
    model_config = ConfigDict(extra="forbid")

    created_from: Annotated[datetime | None, AfterValidator(ensure_utc)] = Field(
        default=None, description="Only rows created at/after this UTC timestamp."
    )
    created_to: Annotated[datetime | None, AfterValidator(ensure_utc)] = Field(
        default=None, description="Only rows created at/before this UTC timestamp."
    )
    status: str | None = Field(
        default=None, description="Restrict to one status (flags/reviews only); rejected for other templates."
    )
    scenario_id: UUID | None = Field(
        default=None, description="Restrict to one scenario (where the row references one)."
    )
    task_id: UUID | None = Field(default=None, description="Restrict to one task (flags export only).")
    user_id: UUID | None = Field(
        default=None,
        description="Restrict to one user/red-teamer — the row's author (or the reviewer, for the reviews export).",
    )

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        """Reject an unknown status at request time (→ 422) rather than the worker silently dropping it.

        Symmetric with the list endpoints, whose `status` query param is enum-typed. A status valid in
        one domain but inapplicable to the chosen template is still mapped per template by `coerce_status`.
        """
        if value is not None and value not in _KNOWN_STATUSES:
            msg = f"Unknown status {value!r}; expected one of {sorted(_KNOWN_STATUSES)}."
            raise ValueError(msg)
        return value

    def active_dimensions(self) -> set[str]:
        """Names of the filter dimensions the caller actually set (non-`None`).

        Matches exactly what the create endpoint persists (`model_dump(exclude_none=True)`), so it's
        the right set to check against a template's `supported_filters`.
        """
        return set(self.model_dump(exclude_none=True))
