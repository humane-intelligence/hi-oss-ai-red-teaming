"""Message-flag endpoints — flat `/message-flags` resource.

A message flag marks one model response as exploit-worthy. It is owner-scoped
(authored by the red-teamer who owns the conversation), so reads gate on
`flags:read` and writes on the matching `flags:{create,update,delete}`; the
service scopes every read/write to the caller's own flags *and* the parent
group's visibility, with the `evaluation_groups:manage` break-glass lifting both
so an admin can reach any user's flag. The flagged message's ancestry
(conversation / evaluation / group / scenario) is resolved from `message_id` at
create time, so it isn't in the payload.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.annotations.dependencies import MessageFlagFiltersDep
from app.core.annotations.dependencies import require_flag_create_permission
from app.core.annotations.dependencies import require_flag_list_permission
from app.core.annotations.dependencies import require_flag_permission
from app.core.annotations.filters import MessageFlagOrderBy
from app.core.annotations.models import MessageFlag
from app.core.annotations.schemas import MessageFlagCreate
from app.core.annotations.schemas import MessageFlagResponse
from app.core.annotations.schemas import MessageFlagUpdate
from app.core.annotations.schemas import MessageFlagUpdateChanges
from app.core.annotations.services.message_flags import create_flag
from app.core.annotations.services.message_flags import get_flag
from app.core.annotations.services.message_flags import get_restorable_flag
from app.core.annotations.services.message_flags import list_flags
from app.core.annotations.services.message_flags import restore_flag
from app.core.annotations.services.message_flags import soft_delete_flag
from app.core.annotations.services.message_flags import update_flag
from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
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

router = APIRouter(prefix="/message-flags", tags=["message-flags"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response(
    "Conversation or flag does not exist (or is not the caller's / not visible), "
    "a selected message is not part of the conversation, "
    "or the task is not part of the conversation's scenario."
)

_NOT_RESTORABLE = problem_response(
    "No restorable flag with this id: never deleted, deleted longer than the restore window ago, "
    "deleted by another user, or hidden behind a soft-deleted conversation."
)

# Each gate accepts the permission from the JWT or from the caller's roles in the group the
# request names — the filters for the listing, the flag itself for the item routes, the target
# conversation for a create.
_ListCaller = Annotated[SessionUser, Depends(require_flag_list_permission(Permission.FLAGS_READ))]
_ReadCaller = Annotated[SessionUser, Depends(require_flag_permission(Permission.FLAGS_READ))]
_CreateCaller = Annotated[SessionUser, Depends(require_flag_create_permission(Permission.FLAGS_CREATE))]
_UpdateCaller = Annotated[SessionUser, Depends(require_flag_permission(Permission.FLAGS_UPDATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_flag_permission(Permission.FLAGS_DELETE))]


_OrderBy = Annotated[
    MessageFlagOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


def _flag_snapshot(flag: MessageFlag) -> dict[str, object]:
    """Curated snapshot for the audit before/after — the flag's editable content + status."""
    return {
        "reason": flag.reason,
        "red_flagged": flag.red_flagged,
        "comment": flag.comment,
        "status": str(flag.status),
    }


@router.post(
    "",
    response_model=MessageFlagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Flag messages",
    description=(
        "Flag a selection of a conversation's messages as exploit-worthy — one, a range, or the whole "
        "conversation. The owner is the authenticated caller; the conversation must be reachable, every "
        "`message_ids` entry must belong to it, and `task_id` (when given) must be a task of its scenario."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def create_message_flag_endpoint(
    payload: MessageFlagCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
    settings: SettingsDep,
) -> MessageFlagResponse:
    """Flag a selection of a conversation's messages, owned by the caller.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:create`.
    * **404 Not Found** — the conversation isn't reachable, a message isn't part of it, or the task isn't
      part of the conversation's scenario.
    """
    flag = await create_flag(db, payload, caller_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.FLAG_CREATE,
        object_type="message_flag",
        object_id=flag.id,
        after=_flag_snapshot(flag),
    )
    response.headers["Location"] = f"/api/v1/message-flags/{flag.id}"
    return MessageFlagResponse.from_model(flag, settings=settings)


@router.get(
    "",
    response_model=Page[MessageFlagResponse],
    status_code=status.HTTP_200_OK,
    summary="List message flags",
    description=(
        "Flat, paginated list of the caller's message flags; filter by `conversation_id`, `evaluation_id`, "
        "`evaluation_group_id`, `scenario_id`, `message_id`, `status`, and/or `red_flagged`. "
        "Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_message_flags_endpoint(
    caller: _ListCaller,
    pagination: PaginationDep,
    filters: MessageFlagFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "-created_at",
) -> Page[MessageFlagResponse]:
    """List the caller's message flags.

    Filters can only narrow the owner-scoped set, never widen it; a filter
    pointing at a resource the caller can't see yields an empty page, not a 404.
    Admins (`evaluation_groups:manage`) see every user's flags.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:read`.
    """
    flags, total = await list_flags(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[MessageFlagResponse](
        items=[MessageFlagResponse.from_model(flag, settings=settings) for flag in flags],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{flag_id}",
    response_model=MessageFlagResponse,
    status_code=status.HTTP_200_OK,
    summary="Get message flag",
    description="Fetch one of the caller's message flags by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_message_flag_endpoint(
    flag_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
    settings: SettingsDep,
) -> MessageFlagResponse:
    """Fetch one message flag.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:read`.
    * **404 Not Found** — no such flag authored by the caller (or visible to them).
    """
    flag = await get_flag(db, flag_id, caller_id=caller.id, can_manage=_can_manage(caller))
    return MessageFlagResponse.from_model(flag, settings=settings)


@router.patch(
    "/{flag_id}",
    response_model=MessageFlagResponse,
    status_code=status.HTTP_200_OK,
    summary="Update message flag",
    description=(
        "Update a flag's content (`reason`, `red_flagged`, `comment`). The flagged message, ancestry, "
        "and review `status` are not editable. Omitted fields stay; send `comment: null` to clear the note."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_message_flag_endpoint(
    flag_id: UUID,
    payload: MessageFlagUpdate,
    caller: _UpdateCaller,
    db: DbSession,
    settings: SettingsDep,
) -> MessageFlagResponse:
    """Update a message flag's content.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:update`.
    * **404 Not Found** — no such flag authored by the caller (or visible to them).
    """
    flag = await get_flag(db, flag_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True)
    before = _flag_snapshot(flag)
    changes = MessageFlagUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_flag(db, flag, changes)
    diff_before, diff_after = changed_fields(before, _flag_snapshot(updated))
    if diff_before or diff_after:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.FLAG_UPDATE,
            object_type="message_flag",
            object_id=updated.id,
            before=diff_before,
            after=diff_after,
        )
    return MessageFlagResponse.from_model(updated, settings=settings)


@router.delete(
    "/{flag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete message flag",
    description="Soft-delete a message flag (sets `deleted_at`); subsequent reads exclude it.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_message_flag_endpoint(
    flag_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a message flag.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:delete`.
    * **404 Not Found** — no such flag authored by the caller (or visible to them).
    """
    flag = await get_flag(db, flag_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True)
    before = _flag_snapshot(flag)
    await soft_delete_flag(db, flag, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.FLAG_DELETE,
        object_type="message_flag",
        object_id=flag.id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{flag_id}/restore",
    response_model=MessageFlagResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore message flag",
    description=(
        "Clear a soft-deleted flag's `deleted_at`, bringing it back into the live listings. "
        "Restorable for a fixed window after the delete (set per deployment), and only by "
        "the user who deleted it — the `evaluation_groups:manage` break-glass restores anyone's."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
    },
)
@transactional
async def restore_message_flag_endpoint(
    flag_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> MessageFlagResponse:
    """Restore a soft-deleted message flag.

    Gated on `flags:delete` — undoing your own delete needs no authority beyond
    the delete itself.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `flags:delete`.
    * **404 Not Found** — no restorable flag: never deleted, deleted longer than
      the restore window ago, deleted by someone else, or hidden behind a
      soft-deleted conversation.
    """
    flag = await get_restorable_flag(
        db,
        flag_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        deleted_cutoff=restore_cutoff(settings),
    )
    restored = await restore_flag(db, flag)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.FLAG_RESTORE,
        object_type="message_flag",
        object_id=restored.id,
        after=_flag_snapshot(restored),
    )
    return MessageFlagResponse.from_model(restored, settings=settings)
