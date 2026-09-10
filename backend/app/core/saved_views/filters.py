"""Query-time filter and sort schema for the saved-views list endpoint.

`SavedViewFilters` is injected via `Depends()` so each field becomes an
individually documented query parameter; `SavedViewOrderBy` is a `Literal`
whitelist of orderable columns (leading `-` selects descending).
"""

from typing import Annotated
from typing import Literal

from fastapi import Query
from pydantic import BaseModel

from app.core.restore import DeletedFilterOwnerOnly
from app.core.saved_views.enums import SavedViewResource

SavedViewOrderBy = Literal[
    "name",
    "-name",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class SavedViewFilters(BaseModel):
    """Optional filters for `GET /v1/saved-views`.

    The owner scope (only the caller's own views) is enforced by the service
    regardless of these filters, so a filter can never widen visibility.
    """

    resource: Annotated[
        SavedViewResource | None,
        Query(description="Restrict to views of one list resource."),
    ] = None
    deleted: DeletedFilterOwnerOnly = False
