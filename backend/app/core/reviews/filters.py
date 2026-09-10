"""Query-time filters and sort schema for the review list + queue endpoints.

`ReviewFilters` / `ReviewQueueFilters` are injected via `Depends()` so each field
becomes an individually documented query parameter; `ReviewOrderBy` is a
`Literal` whitelist of orderable columns (leading `-` selects descending).
"""

from datetime import datetime
from typing import Annotated
from typing import Literal
from uuid import UUID

from fastapi import Query
from pydantic import AfterValidator
from pydantic import BaseModel

from app.core.helpers import ensure_utc
from app.core.reviews.enums import ReviewStatus

ReviewOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class ReviewFilters(BaseModel):
    """Optional filters for the review list endpoints.

    Every field defaults to `None` meaning "no constraint". The visibility +
    reviewer scope is enforced by the service regardless, so a filter can never
    widen it.
    """

    message_flag_id: Annotated[UUID | None, Query(description="Restrict to one submission's reviews.")] = None
    evaluation_id: Annotated[UUID | None, Query(description="Restrict to reviews within one evaluation.")] = None
    reviewer_id: Annotated[UUID | None, Query(description="Restrict to one reviewer's reviews.")] = None
    status: Annotated[ReviewStatus | None, Query(description="Restrict to one review status.")] = None
    created_from: Annotated[
        datetime | None,
        Query(description="Only reviews created at/after this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    created_to: Annotated[
        datetime | None,
        Query(description="Only reviews created at/before this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None


class ReviewQueueFilters(BaseModel):
    """Optional filters for the awaiting-review queue.

    Narrow the queue to one evaluation, group, or scenario, or to the flags no
    reviewer is assigned to. The visibility + reviewer scope is enforced by the
    service regardless.
    """

    evaluation_id: Annotated[UUID | None, Query(description="Restrict to flags within one evaluation.")] = None
    evaluation_group_id: Annotated[UUID | None, Query(description="Restrict to flags within one evaluation group.")] = (
        None
    )
    scenario_id: Annotated[UUID | None, Query(description="Restrict to flags targeting one scenario.")] = None
    unassigned: Annotated[bool, Query(description="Only flags that carry no live reviewer assignment.")] = False
