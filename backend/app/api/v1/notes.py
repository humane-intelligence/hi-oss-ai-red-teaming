"""Note endpoints — flat `/notes` resource.

A note is an annotator's free-text remark on a selection of one conversation's
messages, independent of whether a participant flagged them. Reads *and* the by-id
writes are owner-scoped (the author, plus the `evaluation_groups:manage` break-glass
each handler derives for itself — which lifts the author predicate, so a manager can
also edit and soft-delete another author's note), but **authoring is not**: `POST`
requires only that the conversation's group is visible to the caller, so an annotator
can leave a note on a red-teamer's transcript. The ancestry (evaluation / group) is resolved
from `conversation_id` at create time, so it is not in the payload, and the message
selection is fixed once written.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.annotations.dependencies import NoteFiltersDep
from app.core.annotations.filters import NoteOrderBy
from app.core.annotations.models import Note
from app.core.annotations.schemas import NoteCreate
from app.core.annotations.schemas import NoteResponse
from app.core.annotations.schemas import NoteUpdate
from app.core.annotations.schemas import NoteUpdateChanges
from app.core.annotations.services.notes import create_note
from app.core.annotations.services.notes import get_note
from app.core.annotations.services.notes import get_restorable_note
from app.core.annotations.services.notes import list_notes
from app.core.annotations.services.notes import restore_note
from app.core.annotations.services.notes import soft_delete_note
from app.core.annotations.services.notes import update_note
from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
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

router = APIRouter(prefix="/notes", tags=["notes"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
# Each route declares only the 404 it can produce: `POST` fails on the conversation or
# the selection, the by-id verbs on the note.
_CREATE_NOT_FOUND = problem_response(
    "Conversation does not exist or is not visible to the caller, or a selected message is not part of it."
)
_NOT_FOUND = problem_response("Note does not exist, or is not the caller's / not visible to them.")

_NOT_RESTORABLE = problem_response(
    "No restorable note with this id: never deleted, deleted longer than the restore window ago, "
    "deleted by another user, or hidden behind a soft-deleted conversation."
)

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTES_READ))]
_CreateCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTES_CREATE))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTES_UPDATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_permission(Permission.NOTES_DELETE))]

_OrderBy = Annotated[
    NoteOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


def _note_snapshot(note: Note) -> dict[str, object]:
    """Curated snapshot for the audit before/after — the note's editable content."""
    return {"text": note.text}


def _note_context(note: Note) -> dict[str, str]:
    """Ancestry recorded alongside every note audit row.

    Authoring is not ownership-scoped, so *which* transcript was noted is the
    fact the trail exists to answer, and it is not derivable from the actor. The
    `notes` row cannot be relied on to supply it later: a soft-deleted
    ancestor makes the row unreachable through the API, and a hard-deleted one
    cascades it away.
    """
    return {
        "conversation_id": str(note.conversation_id),
        "evaluation_id": str(note.evaluation_id),
        "evaluation_group_id": str(note.evaluation_group_id),
    }


@router.post(
    "",
    response_model=NoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Write note",
    description=(
        "Attach a free-text note to a selection of one conversation's messages — one, a range, or "
        "the whole conversation. The caller need **not** own the conversation: it is enough that the "
        "conversation's evaluation group is visible to them. The evaluation/group ancestry is "
        "resolved from `conversation_id`; the selection is fixed once written."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _CREATE_NOT_FOUND,
    },
)
@transactional
async def create_note_endpoint(
    payload: NoteCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
) -> NoteResponse:
    """Attach a note to a selection of a conversation's messages.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:create`.
    * **404 Not Found** — the conversation is not visible to the caller, or a selected
      message is not part of it.
    """
    note = await create_note(db, payload, caller_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.NOTE_CREATE,
        object_type="note",
        object_id=note.id,
        # The selection is fixed at create, so its size is recordable only here.
        after=_note_snapshot(note) | {"message_count": len(note.messages)},
        context=_note_context(note),
    )
    response.headers["Location"] = f"/api/v1/notes/{note.id}"
    return NoteResponse.from_model(note)


@router.get(
    "",
    response_model=Page[NoteResponse],
    status_code=status.HTTP_200_OK,
    summary="List notes",
    description=(
        "Flat, paginated list of the notes the caller authored — a holder of "
        "`evaluation_groups:manage` sees every author's. Filter by `conversation_id`, "
        "`evaluation_id`, `evaluation_group_id`, `message_id`, `created_by_id`, the "
        "`created_from`/`created_to` window, and/or a `search` substring of the note; a filter "
        "naming something the caller cannot see narrows the page to nothing rather than 404ing. "
        "Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_notes_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: NoteFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "-created_at",
) -> Page[NoteResponse]:
    """List the caller's notes.

    Filters can only narrow the author-scoped set, never widen it; a filter pointing at
    a resource the caller can't see yields an empty page, not a 404. Admins
    (`evaluation_groups:manage`) see every author's notes.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:read`.
    """
    notes, total = await list_notes(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[NoteResponse](
        items=[NoteResponse.from_model(note) for note in notes],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{note_id}",
    response_model=NoteResponse,
    status_code=status.HTTP_200_OK,
    summary="Get note",
    description=(
        "Fetch one note the caller authored, with the ids of its noted messages. A holder "
        "of `evaluation_groups:manage` may fetch any author's."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_note_endpoint(
    note_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> NoteResponse:
    """Fetch one note.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:read`.
    * **404 Not Found** — no such note authored by the caller (or visible to them).
    """
    note = await get_note(db, note_id, caller_id=caller.id, can_manage=_can_manage(caller))
    return NoteResponse.from_model(note)


@router.patch(
    "/{note_id}",
    response_model=NoteResponse,
    status_code=status.HTTP_200_OK,
    summary="Update note",
    description=(
        "Update a note's `text`. The conversation anchor, the noted message set and the "
        "ancestry are not editable — a different selection is a new note, and sending "
        "`message_ids` here is rejected rather than ignored."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_note_endpoint(
    note_id: UUID,
    payload: NoteUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> NoteResponse:
    """Update a note's text.

    An empty body is accepted and changes nothing — including the audit trail, which
    records only real changes.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:update`.
    * **404 Not Found** — no such note authored by the caller (or visible to them).
    """
    if not payload.model_fields_set:
        # Nothing to write, so take no row lock: a no-op PATCH would otherwise queue
        # behind — and hold up — a concurrent write to the same note.
        return NoteResponse.from_model(await get_note(db, note_id, caller_id=caller.id, can_manage=_can_manage(caller)))
    # The locked read must be the *first* load of this row: taking the `before` snapshot
    # from an earlier unlocked read would seat a stale instance in the identity map and
    # this re-read would hand it back, defeating the lock.
    note = await get_note(db, note_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True)
    before = _note_snapshot(note)
    changes = NoteUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_note(db, note, changes)
    diff_before, diff_after = changed_fields(before, _note_snapshot(updated))
    if diff_before or diff_after:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.NOTE_UPDATE,
            object_type="note",
            object_id=updated.id,
            before=diff_before,
            after=diff_after,
            context=_note_context(updated),
        )
    return NoteResponse.from_model(updated)


@router.delete(
    "/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete note",
    description=(
        "Soft-delete a note (sets `deleted_at`); subsequent reads exclude it. Delete it "
        "while its conversation, evaluation and group are all live — once any of them is deleted "
        "the note is already hidden from every reader, and this route can no longer reach it."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_note_endpoint(
    note_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a note.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:delete`.
    * **404 Not Found** — no such note authored by the caller (or visible to them).
    """
    note = await get_note(db, note_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True)
    before = _note_snapshot(note)
    context = _note_context(note)
    await soft_delete_note(db, note, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.NOTE_DELETE,
        object_type="note",
        object_id=note_id,
        before=before,
        context=context,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{note_id}/restore",
    response_model=NoteResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore note",
    description=(
        "Clear a soft-deleted note's `deleted_at`, bringing it back into the live listings. "
        "Restorable for a fixed window after the delete (set per deployment), and only by the "
        "user who deleted it — the `evaluation_groups:manage` break-glass restores anyone's."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
    },
)
@transactional
async def restore_note_endpoint(
    note_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> NoteResponse:
    """Restore a soft-deleted note.

    Gated on `notes:delete` — undoing your own delete needs no authority
    beyond the delete itself.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `notes:delete`.
    * **404 Not Found** — no restorable note: never deleted, deleted longer
      than the restore window ago, deleted by someone else, or hidden behind a
      soft-deleted conversation.
    """
    note = await get_restorable_note(
        db,
        note_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        deleted_cutoff=restore_cutoff(settings),
    )
    restored = await restore_note(db, note)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.NOTE_RESTORE,
        object_type="note",
        object_id=restored.id,
        after=_note_snapshot(restored),
        context=_note_context(restored),
    )
    return NoteResponse.from_model(restored)
