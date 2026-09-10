"""Query-time filters and sort schema for the `/v1/organizations` list endpoint.

Mirrors the `/v1/auth/users` convention (see `.claude/skills/api/SKILL.md`):
filters are injected via `Depends()` so each becomes a documented query
parameter, and `OrganizationOrderBy` is a `Literal` whitelist of orderable
columns (leading `-` = descending).
"""

from typing import Annotated
from typing import Literal
from typing import Self

from fastapi import Query
from pydantic import BaseModel
from pydantic import model_validator

from app.core.helpers import escape_like

OrganizationOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "name",
    "-name",
    "deleted_at",
    "-deleted_at",
]


class OrganizationFilters(BaseModel):
    """Optional filters for `GET /v1/organizations`. Every field defaults to `None` (no constraint)."""

    name: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against the organization name.", max_length=255),
    ] = None

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in substring filter values (model-level — see UserFilters)."""
        if self.name is not None:
            self.name = escape_like(self.name)
        return self
