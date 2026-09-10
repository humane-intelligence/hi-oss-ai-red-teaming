"""Shared response and query-parameter schemas reused across API modules.

`Problem` is the RFC 7807 error envelope; every non-2xx response uses it.
`Page[T]` is the canonical pagination wrapper for list endpoints.
`PaginationParams` is the query-parameter pair (`limit`, `offset`) for them.
"""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ProblemErrorItem(BaseModel):
    """One field-level error inside a `Problem.errors` list.

    Mirrors Pydantic's `ValidationError.errors()` shape so 422 responses
    can be forwarded verbatim by the validation handler.
    """

    loc: list[str | int] = Field(
        description="Path to the offending field (e.g. ['body', 'name']).",
        examples=[["body", "name"]],
    )
    msg: str = Field(
        description="Human-readable explanation of what went wrong.",
        examples=["Field required"],
    )
    type: str = Field(
        description="Machine-readable error type from the validator.",
        examples=["missing"],
    )


class Problem(BaseModel):
    """RFC 7807 Problem Details envelope.

    Every error response in this API has this shape and is served with
    `Content-Type: application/problem+json`. Clients should branch on
    `status` and (optionally) `type` rather than parsing `detail`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "User 7f3a not found.",
                "instance": "/api/v1/auth/users/7f3a",
            }
        }
    )

    type: str = Field(
        default="about:blank",
        description="URI identifying the problem type. `about:blank` means generic.",
        examples=["about:blank", "urn:redteam:error:terms-acceptance-required"],
    )
    title: str = Field(
        description="Short, human-readable summary. Stable per `type`.",
        examples=["Not Found"],
    )
    status: int = Field(
        description="HTTP status code; mirrors the response status.",
        examples=[404],
    )
    detail: str | None = Field(
        default=None,
        description="Human-readable explanation specific to this occurrence.",
        examples=["User 7f3a not found."],
    )
    instance: str | None = Field(
        default=None,
        description="URI reference identifying the specific occurrence (typically the request path).",
        examples=["/api/v1/auth/users/7f3a"],
    )
    errors: list[ProblemErrorItem] | None = Field(
        default=None,
        description=(
            "Field-level errors keyed to the offending request fields. Present on 422 "
            "schema-validation failures and on field-addressable business-rule errors "
            "(e.g. the password-policy 400)."
        ),
    )


class Page[T](BaseModel):
    """Generic pagination wrapper used by every list endpoint.

    `limit` and `offset` echo the request so clients can paginate without
    tracking state. `total` is the unfiltered count so UIs can render
    page indicators.
    """

    items: list[T] = Field(description="Items on this page.")
    total: int = Field(description="Total number of items across all pages.", examples=[42])
    limit: int = Field(description="Max items per page (echo of the request).", examples=[20])
    offset: int = Field(description="Zero-based offset of the first item on this page.", examples=[0])


class PaginationParams(BaseModel):
    """Query parameters for paginated list endpoints.

    Inject via `PaginationDep` from `app.core.dependencies`; do not declare
    `limit` / `offset` on individual routes — keep the contract uniform.
    """

    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Max items to return (1..100)."),
    ] = 20
    offset: Annotated[
        int,
        Query(ge=0, description="Zero-based offset into the result set."),
    ] = 0
