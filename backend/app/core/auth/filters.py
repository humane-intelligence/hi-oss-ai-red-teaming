"""Query-time filters and sort schema for the `/v1/auth/users` list endpoint.

Per the API convention (see `.claude/skills/api/SKILL.md`):

* `UserFilters` is injected via `Depends()` so every filter becomes an
  individually documented query parameter on the OpenAPI spec.
* `UserOrderBy` is a `Literal` of the columns clients may order by, with a
  leading `-` selecting descending order (JSON:API / Stripe convention).
  Keeping the whitelist in the type means the spec advertises exactly
  what the service will accept.
"""

from typing import Annotated
from typing import Literal
from typing import Self
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel
from pydantic import model_validator

from app.core.auth.models import UserStatus
from app.core.helpers import escape_like

UserOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "email",
    "-email",
    "status",
    "-status",
    "first_name",
    "-first_name",
    "last_name",
    "-last_name",
    "deleted_at",
    "-deleted_at",
]


class UserFilters(BaseModel):
    """Optional filters for `GET /v1/auth/users`.

    Every field defaults to `None` meaning "no constraint". Keep this
    surface narrow and aligned with indexed columns; broaden only when a
    real client needs it.
    """

    status: Annotated[
        UserStatus | None,
        Query(description="Restrict to one lifecycle state."),
    ] = None
    email: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against email.", max_length=320),
    ] = None
    first_name: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against first name.", max_length=255),
    ] = None
    last_name: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against last name.", max_length=255),
    ] = None
    role_id: Annotated[
        UUID | None,
        Query(description="Restrict to users holding this role."),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against email OR first OR last name.", max_length=320),
    ] = None

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in substring filter values.

        Done at the model level (rather than per-field `AfterValidator`)
        because FastAPI's `Depends()` validates each Query parameter
        independently *and* re-validates during model construction —
        a field-level wrapper would run twice and double-escape the input.
        """
        if self.email is not None:
            self.email = escape_like(self.email)
        if self.first_name is not None:
            self.first_name = escape_like(self.first_name)
        if self.last_name is not None:
            self.last_name = escape_like(self.last_name)
        if self.search is not None:
            self.search = escape_like(self.search)
        return self
