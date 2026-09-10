"""Annotator search endpoint — `/api/v1/evaluation-groups/{group_id}/annotators`.

Lists the reviewers a manager can pick when assigning a flag or review within
the group — holders of the `reviews:annotate` capability (a live, active role
granting it, not a role name). A public group draws from all global holders plus
the group's in-group holders; an organization group from its org's global holders
plus in-group holders (so an invited external reviewer stays assignable); an
invitation-only group from its in-group holders only. Gated on the object-scope
`evaluation_groups:manage_members` permission (the in-group `owner` role or a
break-glass admin) — picking a reviewer is a group-management action, and the
public branch would otherwise expose the full reviewer roster.
"""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Query
from fastapi import status

from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.evaluations.dependencies import ManageMembersDep
from app.core.evaluations.schemas import AnnotatorResponse
from app.core.evaluations.services.annotators import list_group_annotators
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/evaluation-groups/{group_id}/annotators", tags=["evaluation-groups"])


@router.get(
    "",
    response_model=Page[AnnotatorResponse],
    status_code=status.HTTP_200_OK,
    summary="List assignable annotators",
    description="Return a paginated list of annotators that can be assigned to a flag or review in this group.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks permission to manage this group's members."),
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation group not found, or not visible to the caller."),
    },
)
async def list_group_annotators_endpoint(
    context: ManageMembersDep,
    pagination: PaginationDep,
    db: DbSession,
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on annotator email / first / last name.", max_length=320),
    ] = None,
) -> Page[AnnotatorResponse]:
    """List the reviewers assignable to a flag or review in this group.

    Candidates hold the `reviews:annotate` capability (a live, active role
    granting it, not a role name), scoped by access level: a **public** group
    lists all global holders plus in-group holders; an **organization** group the
    org's global holders plus in-group holders (so an invited external reviewer is
    still assignable); an **invitation-only** group the in-group holders only.
    Gated on `evaluation_groups:manage_members` (the in-group `owner` or a
    break-glass admin).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    """
    annotators, total = await list_group_annotators(
        db, context.group, search=search, limit=pagination.limit, offset=pagination.offset
    )
    return Page[AnnotatorResponse](
        items=[AnnotatorResponse.from_user(user) for user in annotators],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
