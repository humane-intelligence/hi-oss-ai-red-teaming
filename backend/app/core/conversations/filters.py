"""Query-time filters and sort schemas for the conversation list endpoint.

`ConversationFilters` is injected via `Depends()` so each field becomes an
individually documented query parameter; `ConversationOrderBy` is a `Literal`
whitelist of orderable columns (leading `-` selects descending).
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

from app.core.helpers import ensure_utc
from app.core.helpers import escape_like
from app.core.restore import DeletedFilter

ConversationOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "title",
    "-title",
    "deleted_at",
    "-deleted_at",
]

ConversationGroupOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
]

# Shared so the nested route's hand-built `scenario_id` query param and this
# filter field carry the *same* description from one place — no drift.
ScenarioIdFilter = Annotated[
    UUID | None,
    Query(description="Restrict to conversations targeting one scenario."),
]

ConversationGroupIdFilter = Annotated[
    UUID | None,
    Query(description="Restrict to conversations in one group."),
]

GroupScenarioIdFilter = Annotated[
    UUID | None,
    Query(description="Restrict to groups targeting one scenario."),
]

TitleFilter = Annotated[
    str | None,
    Query(description="Case-insensitive substring match on title.", max_length=255),
]

# Shared across the conversation + conversation-group filters (both carry `user_id` + `created_at`).
UserIdFilter = Annotated[UUID | None, Query(description="Restrict to one user's (owner's) rows.")]
CreatedFromFilter = Annotated[
    datetime | None, Query(description="Only rows created at/after this UTC timestamp."), AfterValidator(ensure_utc)
]
CreatedToFilter = Annotated[
    datetime | None, Query(description="Only rows created at/before this UTC timestamp."), AfterValidator(ensure_utc)
]


class ConversationFilters(BaseModel):
    """Optional filters for `GET /v1/conversations`.

    Every field defaults to `None` meaning "no constraint". The owner scope
    (only the caller's own conversations, unless break-glass) is enforced by the
    service regardless of these filters, so a filter can never widen visibility.
    """

    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to conversations against one evaluation."),
    ] = None
    scenario_id: ScenarioIdFilter = None
    conversation_group_id: ConversationGroupIdFilter = None
    title: TitleFilter = None
    user_id: UserIdFilter = None
    created_from: CreatedFromFilter = None
    created_to: CreatedToFilter = None
    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `title` (see `EvaluationFilters`).

        Done at the model level, not a per-field `AfterValidator` — FastAPI's
        `Depends()` re-validates during model construction, which would double-escape
        a field-level wrapper.
        """
        if self.title is not None:
            self.title = escape_like(self.title)
        return self


class ConversationGroupFilters(BaseModel):
    """Optional filters for the flat `GET /v1/conversation-groups` list.

    Like `ConversationFilters`, the owner + group-visibility scope is enforced by
    the service regardless of these, so a filter can never widen visibility.
    """

    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to groups against one evaluation."),
    ] = None
    scenario_id: GroupScenarioIdFilter = None
    user_id: UserIdFilter = None
    created_from: CreatedFromFilter = None
    created_to: CreatedToFilter = None
