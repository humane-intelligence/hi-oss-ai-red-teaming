"""Annotation endpoints — flat `/annotations` resource.

An annotation is one label on one message from one author, picked from the shared
vocabulary or typed ad hoc. Reads are deliberately **shared**: any holder of
`annotations:read` sees every annotation in a group visible to them — a tag exists
to be aggregated, so author-scoping reads would defeat it. Authoring needs only that
the message's group is visible (an annotator labels a red-teamer's message); deleting
stays author-scoped, refused with **403** for another author's row — its existence is
already public to the caller, so there is nothing to hide — with the
`evaluation_groups:manage` break-glass lifting it. There is no update: the row has no
editable content, so changing the label is delete + re-create.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.annotations.dependencies import AnnotationFiltersDep
from app.core.annotations.filters import AnnotationOrderBy
from app.core.annotations.models import Annotation
from app.core.annotations.schemas import AnnotationCreate
from app.core.annotations.schemas import AnnotationResponse
from app.core.annotations.services.annotations import assert_may_delete_annotation
from app.core.annotations.services.annotations import create_annotation
from app.core.annotations.services.annotations import get_annotation
from app.core.annotations.services.annotations import get_restorable_annotation
from app.core.annotations.services.annotations import list_annotations
from app.core.annotations.services.annotations import restore_annotation
from app.core.annotations.services.annotations import soft_delete_annotation
from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/annotations", tags=["annotations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_DELETE_FORBIDDEN = problem_response(
    "Caller lacks the 'annotations:delete' permission, or is not the annotation's author."
)
_CREATE_NOT_FOUND = problem_response(
    "Message does not exist or is not visible to the caller, or the label does not exist live."
)
_NOT_FOUND = problem_response("Annotation does not exist, or is not visible to the caller.")
_NOT_RESTORABLE = problem_response(
    "No restorable annotation with this id: never deleted, deleted longer than the restore window "
    "ago, deleted by another user, or hidden behind a soft-deleted conversation."
)
_RESTORE_CONFLICT = problem_response("An identical annotation already exists.")

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.ANNOTATIONS_READ))]
_CreateCaller = Annotated[SessionUser, Depends(require_permission(Permission.ANNOTATIONS_CREATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_permission(Permission.ANNOTATIONS_DELETE))]

_OrderBy = Annotated[
    AnnotationOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


def _annotation_snapshot(annotation: Annotation) -> dict[str, object]:
    """Snapshot for the audit before/after — the label the row carries (never edited)."""
    return {
        "message_id": str(annotation.message_id),
        "label_id": str(annotation.label_id),
        # The wording too: a label the author typed is theirs alone, so an admin reading the
        # trail cannot look it up in the shared vocabulary.
        "label_name": annotation.label.name,
    }


def _annotation_context(annotation: Annotation) -> dict[str, str]:
    """Ancestry recorded alongside every annotation audit row.

    Authoring is not ownership-scoped, so *which* transcript was labelled is the fact
    the trail exists to answer, and the row cannot be relied on to supply it later — a
    soft-deleted ancestor makes it unreachable, a hard-deleted one cascades it away.
    """
    return {
        "conversation_id": str(annotation.conversation_id),
        "evaluation_id": str(annotation.evaluation_id),
        "evaluation_group_id": str(annotation.evaluation_group_id),
    }


@router.post(
    "",
    response_model=AnnotationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Annotate message",
    description=(
        "Label one message — with an existing label (`label_id`) or by naming one (`text`), "
        "exactly one of the two. A named label resolves to the curated row of that wording if one "
        "exists, else the caller's own, else a new one scoped to them — so picking a label and "
        "typing its name reach the same row. A `label_id` naming a colleague's label already used "
        "on this conversation resolves to the caller's own row of the same wording. The caller "
        "need **not** own the "
        "conversation: it is enough that its evaluation group is visible to them. Re-annotating "
        "the same message with the same label returns the existing annotation rather than "
        "conflicting."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _CREATE_NOT_FOUND,
    },
)
@transactional
async def create_annotation_endpoint(
    payload: AnnotationCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
) -> AnnotationResponse:
    """Label a message, idempotently.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:create`.
    * **404 Not Found** — the message is not visible to the caller, or `label_id`
      names no label this caller may attach.
    """
    annotation, created = await create_annotation(db, payload, caller_id=caller.id)
    # A repeat returns the existing row, so auditing it again would put a second
    # "created" entry for one annotation on an admin-visible surface.
    if created:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.ANNOTATION_CREATE,
            object_type="annotation",
            object_id=annotation.id,
            after=_annotation_snapshot(annotation),
            context=_annotation_context(annotation),
        )
    response.headers["Location"] = f"/api/v1/annotations/{annotation.id}"
    return AnnotationResponse.from_model(annotation)


@router.get(
    "",
    response_model=Page[AnnotationResponse],
    status_code=status.HTTP_200_OK,
    summary="List annotations",
    description=(
        "Flat, paginated list of the annotations in groups visible to the caller — **every** "
        "author's, deliberately: a tag exists to be aggregated. Filter by `message_id`, "
        "`conversation_id`, `evaluation_id`, `evaluation_group_id`, `label_id`, `created_by_id`, "
        "and/or the `created_from`/`created_to` window; a filter naming "
        "something the caller cannot see narrows the page to nothing rather than 404ing. "
        "Annotations on superseded messages keep listing. Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_annotations_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: AnnotationFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "-created_at",
) -> Page[AnnotationResponse]:
    """List the annotations visible to the caller.

    `deleted=true` swaps in the caller's own tombstones inside the restore window — the
    restore surface stays personal even though live reads are shared.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:read`.
    """
    annotations, total = await list_annotations(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[AnnotationResponse](
        items=[AnnotationResponse.from_model(annotation) for annotation in annotations],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{annotation_id}",
    response_model=AnnotationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get annotation",
    description=(
        "One annotation by id — any author's, provided its evaluation group is visible to the "
        "caller. A retired catalog label still renders here: the row embeds it."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_annotation_endpoint(
    annotation_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> AnnotationResponse:
    """Fetch one annotation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:read`.
    * **404 Not Found** — no such annotation in a group visible to the caller.
    """
    annotation = await get_annotation(db, annotation_id, caller_id=caller.id, can_manage=_can_manage(caller))
    return AnnotationResponse.from_model(annotation)


@router.delete(
    "/{annotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete annotation",
    description=(
        "Soft-delete an annotation (sets `deleted_at`), freeing its dedup slot. Author-only — "
        "another author's annotation is readable but refuses deletion with **403**, since reads "
        "are shared and its existence is no secret; `evaluation_groups:manage` lifts that."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _DELETE_FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_annotation_endpoint(
    annotation_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete an annotation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:delete`, or is not the author.
    * **404 Not Found** — no such annotation in a group visible to the caller.
    """
    can_manage = _can_manage(caller)
    annotation = await get_annotation(db, annotation_id, caller_id=caller.id, can_manage=can_manage, for_update=True)
    assert_may_delete_annotation(annotation, caller_id=caller.id, can_manage=can_manage)
    before = _annotation_snapshot(annotation)
    context = _annotation_context(annotation)
    await soft_delete_annotation(db, annotation, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ANNOTATION_DELETE,
        object_type="annotation",
        object_id=annotation_id,
        before=before,
        context=context,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{annotation_id}/restore",
    response_model=AnnotationResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore annotation",
    description=(
        "Clear a soft-deleted annotation's `deleted_at`, bringing it back into the live listings. "
        "Restorable for a fixed window after the delete, and only by the user who deleted it — "
        "the `evaluation_groups:manage` break-glass restores anyone's. Re-annotating since the "
        "delete can occupy the dedup slot, in which case the restore conflicts."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: _RESTORE_CONFLICT,
    },
)
@transactional
async def restore_annotation_endpoint(
    annotation_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> AnnotationResponse:
    """Restore a soft-deleted annotation.

    Gated on `annotations:delete` — undoing your own delete needs no authority beyond
    the delete itself.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `annotations:delete`.
    * **404 Not Found** — no restorable annotation: never deleted, outside the window,
      deleted by someone else, or hidden behind a soft-deleted conversation.
    * **409 Conflict** — an identical live annotation was created since the delete.
    """
    annotation = await get_restorable_annotation(
        db,
        annotation_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        deleted_cutoff=restore_cutoff(settings),
    )
    restored = await restore_annotation(db, annotation)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.ANNOTATION_RESTORE,
        object_type="annotation",
        object_id=restored.id,
        after=_annotation_snapshot(restored),
        context=_annotation_context(restored),
    )
    return AnnotationResponse.from_model(restored)
