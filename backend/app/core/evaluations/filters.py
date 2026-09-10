"""Query-time filters and sort schemas for the evaluation-domain list endpoints.

Each `*Filters` model is injected via `Depends()` so every field becomes an
individually documented query parameter, and each `*OrderBy` is a `Literal`
whitelist of orderable columns (leading `-` selects descending).
"""

from typing import Annotated
from typing import Literal
from typing import Self
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel
from pydantic import model_validator

from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.helpers import escape_like
from app.core.restore import DeletedFilter

EvaluationGroupOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "title",
    "-title",
    "start_date",
    "-start_date",
    "end_date",
    "-end_date",
]


class EvaluationGroupFilters(BaseModel):
    """Optional filters for `GET /v1/evaluation-groups`.

    The `status` / `access_level` / `search` fields default to `None` meaning
    "no constraint". `all_groups` is a scope toggle rather than a `WHERE` filter:
    it widens the result past the caller's visibility (`public` or member) to every
    group, and is privileged — the route rejects it with 403 unless the caller
    holds `evaluation_groups:manage` (mirrors `AiModelFilters.include_disabled`).
    """

    status: Annotated[
        PublicationStatus | None,
        Query(description="Restrict to one lifecycle state."),
    ] = None
    access_level: Annotated[
        EvaluationGroupAccessLevel | None,
        Query(description="Restrict to one access mode."),
    ] = None
    accepts_evaluations: Annotated[
        bool | None,
        Query(
            description=(
                "Restrict to groups whose lifecycle state accepts new evaluations "
                "(`approved`/`published`); `false` selects the complement."
            ),
        ),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on title or description.", max_length=255),
    ] = None
    all_groups: Annotated[
        bool,
        Query(
            description=(
                "List every evaluation group, not just those visible to the caller "
                "(`public` or one the caller is a member of). Requires the `evaluation_groups:manage` permission; "
                "rejected with 403 otherwise."
            ),
        ),
    ] = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search`.

        Done at the model level (not a per-field `AfterValidator`) because
        FastAPI's `Depends()` re-validates during model construction — a
        field-level wrapper would run twice and double-escape the input.
        """
        if self.search is not None:
            self.search = escape_like(self.search)
        return self


EvaluationOrderBy = Literal[
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "title",
    "-title",
    "status",
    "-status",
    "deleted_at",
    "-deleted_at",
]


class EvaluationFilters(BaseModel):
    """Optional filters for `GET /v1/evaluations`.

    Every field defaults to `None` meaning "no constraint". `evaluation_group_id`
    narrows to a single engagement; visibility (the parent group is public or
    one the caller is a member of) is enforced by the service regardless of this filter.
    """

    evaluation_group_id: Annotated[
        UUID | None,
        Query(description="Restrict to evaluations in one engagement group."),
    ] = None
    status: Annotated[
        EvaluationStatus | None,
        Query(description="Restrict to one lifecycle state."),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on title or description.", max_length=255),
    ] = None
    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search` (see the group filter for why)."""
        if self.search is not None:
            self.search = escape_like(self.search)
        return self


EvaluationAiModelOrderBy = Literal[
    "name",
    "-name",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "deleted_at",
    "-deleted_at",
]


class EvaluationAiModelFilters(BaseModel):
    """Optional filters for `GET /v1/evaluations/{evaluation_id}/models`.

    `search` is masking-aware: the service matches it against `model_display_mask`
    when the parent evaluation masks models, and against the real model name
    otherwise (see `list_evaluation_models`).

    `deleted` swaps in the assignments this evaluation unassigned inside the restore
    window — narrowed to those whose model is still live, since those are the only
    ones `POST …/restore` will take (a dead model cannot be dispatched to).
    """

    search: Annotated[
        str | None,
        Query(
            description=(
                "Case-insensitive substring match on the model name — the display mask when the "
                "evaluation masks models, otherwise the real model name."
            ),
            max_length=255,
        ),
    ] = None

    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search` (see the group filter for why)."""
        if self.search is not None:
            self.search = escape_like(self.search)
        return self


ScenarioOrderBy = Literal[
    "position",
    "-position",
    "created_at",
    "-created_at",
    "name",
    "-name",
    "deleted_at",
    "-deleted_at",
]


class ScenarioFilters(BaseModel):
    """Optional filters for the scenario list endpoints.

    `evaluation_id` drives the standalone `GET /scenarios` cross-evaluation
    view; both fields default to `None` meaning "no constraint".
    """

    evaluation_id: Annotated[
        UUID | None,
        Query(description="Restrict to scenarios of one evaluation."),
    ] = None
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on name or description.", max_length=255),
    ] = None
    deleted: DeletedFilter = False

    @model_validator(mode="after")
    def _escape_like_inputs(self) -> Self:
        """Escape `LIKE`/`ILIKE` metacharacters in `search` (see `EvaluationGroupFilters`)."""
        if self.search is not None:
            self.search = escape_like(self.search)
        return self
