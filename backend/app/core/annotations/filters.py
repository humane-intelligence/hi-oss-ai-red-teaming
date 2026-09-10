"""Query-time filters and sort schemas for the message-flag and note list endpoints.

The filter models are injected via `Depends()` so each field becomes an
individually documented query parameter; the `…OrderBy` aliases are `Literal`
whitelists of orderable columns (leading `-` selects descending).
"""

from datetime import datetime
from typing import Annotated
from typing import Literal
from typing import Self
from uuid import UUID

from fastapi import Query
from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import model_validator

from app.core.annotations.enums import FlagStatus
from app.core.helpers import ensure_utc
from app.core.helpers import escape_like
from app.core.restore import DeletedFilter

MessageFlagOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class MessageFlagFilters(BaseModel):
    """Optional filters for `GET /v1/message-flags`.

    Every field defaults to `None` meaning "no constraint". The owner scope
    (only the caller's own flags, unless break-glass) is enforced by the service
    regardless of these filters, so a filter can never widen visibility.
    """

    conversation_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags on one conversation."),
    ] = None
    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags within one evaluation."),
    ] = None
    evaluation_group_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags within one evaluation group."),
    ] = None
    scenario_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags whose conversation targets one scenario."),
    ] = None
    task_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags whose conversation targets one task."),
    ] = None
    created_by_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags authored by one user (red-teamer)."),
    ] = None
    created_from: Annotated[
        datetime | None,
        Query(description="Only flags created at/after this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    created_to: Annotated[
        datetime | None,
        Query(description="Only flags created at/before this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    message_id: Annotated[
        UUID | None,
        Query(description="Restrict to flags whose selection includes this message."),
    ] = None
    status: Annotated[
        FlagStatus | None,
        Query(description="Restrict to one review status."),
    ] = None
    red_flagged: Annotated[
        bool | None,
        Query(description="Restrict to flags with the given exploit-worthy assertion."),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on reason or comment.", max_length=255),
    ] = None
    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search` (see `ScenarioFilters`)."""
        if self.search is not None:
            self.search = escape_like(self.search)
        return self


NoteOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class NoteFilters(BaseModel):
    """Optional filters for `GET /v1/notes`.

    Every field defaults to `None` meaning "no constraint". The owner scope (only
    the caller's own notes, unless break-glass) is enforced by the service
    regardless of these filters, so a filter can never widen visibility.
    """

    conversation_id: Annotated[
        UUID | None,
        Query(description="Restrict to notes on one conversation."),
    ] = None
    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to notes within one evaluation."),
    ] = None
    evaluation_group_id: Annotated[
        UUID | None,
        Query(description="Restrict to notes within one evaluation group."),
    ] = None
    message_id: Annotated[
        UUID | None,
        Query(description="Restrict to notes whose selection includes this message."),
    ] = None
    created_by_id: Annotated[
        UUID | None,
        Query(
            description=(
                "Restrict to notes authored by one user. Under the owner scope this only ever "
                "matches the caller — naming anyone else yields an empty page, not a 404."
            )
        ),
    ] = None
    created_from: Annotated[
        datetime | None,
        Query(description="Only notes created at/after this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    created_to: Annotated[
        datetime | None,
        Query(description="Only notes created at/before this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on the note text.", max_length=255),
    ] = None
    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search` (see `MessageFlagFilters`)."""
        if self.search is not None:
            self.search = escape_like(self.search)
        return self


AnnotationOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class AnnotationFilters(BaseModel):
    """Optional filters for `GET /v1/annotations`.

    Every field defaults to `None` meaning "no constraint". Group visibility is
    enforced by the service regardless of these filters — reads are deliberately
    *not* author-scoped, so `created_by_id` is an ordinary filter here, not a scope.
    """

    message_id: Annotated[
        UUID | None,
        Query(description="Restrict to annotations on one message."),
    ] = None
    conversation_id: Annotated[
        UUID | None,
        Query(description="Restrict to annotations within one conversation."),
    ] = None
    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to annotations within one evaluation."),
    ] = None
    evaluation_group_id: Annotated[
        UUID | None,
        Query(description="Restrict to annotations within one evaluation group."),
    ] = None
    label_id: Annotated[
        UUID | None,
        Query(description="Restrict to one label, curated or custom."),
    ] = None
    created_by_id: Annotated[
        UUID | None,
        Query(description="Restrict to annotations authored by one user."),
    ] = None
    created_from: Annotated[
        datetime | None,
        Query(description="Only annotations created at/after this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    created_to: Annotated[
        datetime | None,
        Query(description="Only annotations created at/before this UTC timestamp."),
        AfterValidator(ensure_utc),
    ] = None
    deleted: DeletedFilter = False
