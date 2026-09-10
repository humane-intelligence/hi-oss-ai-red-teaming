"""Query-time filters for the `/v1/ai-models` list endpoint.

Per the API convention (see `.claude/skills/api/SKILL.md`): `AiModelFilters`
is injected via `Depends()` so each field becomes an individually documented
query parameter on the OpenAPI spec.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel

from app.core.restore import DeletedFilterNewestFirst


class AiModelFilters(BaseModel):
    """Optional filters for `GET /v1/ai-models`.

    `include_disabled` is privileged: surfacing disabled rows is reserved for
    callers who can re-enable them (`models:update`). The route rejects the
    flag with 403 for read-only callers rather than silently dropping it, so a
    client never believes it received disabled rows when it didn't.

    `assignable_to_evaluation` is a cross-domain filter (it reads the evaluation's
    group subset + existing assignments), so the route — not `_apply_ai_model_filters`
    — resolves it, mirroring how `include_disabled` is partly route-enforced.

    `deleted` is privileged for the same reason as `include_disabled`, and enforced
    the same way (403, not a silent drop): restoring a model needs `models:delete`,
    so browsing the registry's tombstones is reserved for callers who can act on
    them. It also lifts the `include_disabled` default — a disabled row that was
    then deleted is still restorable, so hiding it would leave a restorable model
    no one can see.

    `for_group` is an **authorization** hint, not a filter: it lets an in-group
    `owner` without the global `models:read` permission list the registry to curate
    that group's allowed-model subset. The route authorizes via object-scoped
    `models:read` on the group; it never narrows the result set. It is consulted
    only for callers lacking global `models:read` — a global reader is authorized
    outright, so their `for_group` is neither resolved nor validated.
    """

    include_disabled: Annotated[
        bool,
        Query(
            description=(
                "Include disabled models in the result. Requires the "
                "`models:update` permission; rejected with 403 otherwise."
            ),
        ),
    ] = False
    assignable_to_evaluation: Annotated[
        UUID | None,
        Query(
            description=(
                "Restrict to models assignable to this evaluation: those in the parent group's "
                "allowed-model subset (an empty subset yields none) that are not already assigned "
                "to it. 404 if the evaluation does not exist."
            ),
        ),
    ] = None
    for_group: Annotated[
        UUID | None,
        Query(
            description=(
                "Authorize the request via object-scoped `models:read` on this evaluation group, "
                "for an in-group owner without the global permission. Consulted only when the caller "
                "lacks global `models:read` — a global reader is already authorized, so their `for_group` "
                "is ignored (not validated). Does not filter results; for a caller without global "
                "`models:read`: 403 for a group member who lacks the permission, 404 for a group not "
                "visible to the caller."
            ),
        ),
    ] = None
    deleted: DeletedFilterNewestFirst = False
