"""Annotation-label endpoints — flat, read-only `/annotation-labels` resource.

The shared vocabulary the per-message label picker suggests. These rows are code-owned
(`annotations/label_catalog.py`, reconciled by `sync_annotation_labels`), so there is no write
surface here: changing the *curated* vocabulary is a code change plus a deploy, not an API
call. A label an annotator names while annotating is a row in the same table but is not
curated; it is served here to its own author, and to anyone annotating a conversation it has
been used on, so a spelling is offered back rather than retyped.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status

from app.core.annotations.schemas import AnnotationLabelResponse
from app.core.annotations.services.annotation_labels import list_annotation_labels
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/annotation-labels", tags=["annotation-labels"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")


@router.get(
    "",
    response_model=Page[AnnotationLabelResponse],
    status_code=status.HTTP_200_OK,
    summary="List annotation labels",
    description=(
        "The labels this caller may pick from, ordered by display name: the shared curated "
        "vocabulary, **plus** the caller's own labels (created by naming one while annotating), "
        "plus — with `conversation_id` — those already used in that conversation, whoever typed "
        "them. Each entry carries `is_custom` to tell the two kinds apart. Another annotator's "
        "labels are never listed wholesale; only the ones in a conversation you can see. "
        "**One entry per wording**, since each annotator holds their own row for a spelling they "
        "have used: where several rows share a name, the curated one wins, then yours, then a "
        "colleague's — so `total` counts wordings on offer, not rows in the table. "
        "Read-only: the curated entries are code-owned and reconciled on deploy, and a typed one "
        "is created as a side effect of annotating, so there is no create or edit route here. "
        "Gated on `annotations:read` rather than being open to any authenticated caller (as the "
        "licence pick-list is), so the vocabulary stays out of reach of a role deliberately kept "
        "clear of the whole annotation surface — the red teamer. A role that reads annotations "
        "without authoring them, such as the owner, does hold the key. "
        "Paginated like every list here, so a picker wanting the whole vocabulary should request "
        "`limit=100` and keep paging while `total` exceeds `offset + limit`. There is no search "
        "parameter yet: the curated set is a handful of entries, but a caller's own labels "
        "accumulate, so paging is what a long list needs until one lands. "
        "An empty **first** page means the deploy's sync step has not run; nothing else depends "
        "on these rows, so the picker simply has nothing to suggest."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_annotation_labels_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.ANNOTATIONS_READ))],
    pagination: PaginationDep,
    db: DbSession,
    conversation_id: Annotated[
        UUID | None,
        Query(
            description="Also offer labels already used in this conversation, whoever typed them — "
            "so annotators on one transcript converge on a spelling instead of inventing parallel ones."
        ),
    ] = None,
) -> Page[AnnotationLabelResponse]:
    """List the annotation labels available to pick from.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:read`.
    """
    labels, total = await list_annotation_labels(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        conversation_id=conversation_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[AnnotationLabelResponse](
        items=[AnnotationLabelResponse.from_model(label) for label in labels],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
