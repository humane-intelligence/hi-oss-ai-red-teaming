"""User CRUD endpoints — mounted under `/api/v1/auth/users`."""

from typing import Annotated
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import UserFiltersDep
from app.core.auth.dependencies import require_permission
from app.core.auth.filters import UserOrderBy
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.service import objects_solely_held_by
from app.core.auth.roles import Permission
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.schemas import ForceLogoutTarget
from app.core.auth.schemas import InvitationResponse
from app.core.auth.schemas import PasswordResetBulkRequest
from app.core.auth.schemas import PasswordResetTarget
from app.core.auth.schemas import SessionUser
from app.core.auth.schemas import UserResponse
from app.core.auth.schemas import UserStatusChange
from app.core.auth.schemas import UserStatusTarget
from app.core.auth.schemas import UserUpdate
from app.core.auth.services.invitations import reissue_platform_invitation
from app.core.auth.services.invitations import revoke_platform_invitation
from app.core.auth.services.password_resets import PasswordResetEmailSpec
from app.core.auth.services.password_resets import assert_password_reset_allowed
from app.core.auth.services.password_resets import issue_password_reset
from app.core.auth.services.session_revocation import revoke_user_sessions
from app.core.auth.services.users import UserUpdateChanges
from app.core.auth.services.users import assert_can_delete_user
from app.core.auth.services.users import assert_can_manage_invitation
from app.core.auth.services.users import assert_can_manage_status
from app.core.auth.services.users import assert_can_restore_user
from app.core.auth.services.users import change_user_status
from app.core.auth.services.users import get_restorable_user
from app.core.auth.services.users import get_user
from app.core.auth.services.users import list_users
from app.core.auth.services.users import resolve_assignable_roles
from app.core.auth.services.users import restore_user
from app.core.auth.services.users import soft_delete_user
from app.core.auth.services.users import update_user
from app.core.bulk import BulkRequest
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.email import send_email
from app.core.email import send_email_best_effort
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.logging import get_logger
from app.core.notifications.services.notifications import create_notification
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilterAnyDeleter
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

logger = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("User does not exist.")
_NOT_RESTORABLE = problem_response(
    "No restorable user with this id: never deleted, or deleted longer than the restore window ago."
)


def _user_snapshot(user: User) -> dict[str, object]:
    """Curated, non-secret snapshot for the audit before/after — never the password hash.

    Includes the assigned role names: a role-only PATCH (e.g. granting/removing admin) is the
    most security-relevant change this endpoint makes, so the trail must capture it — without
    them a privilege escalation writes an empty before/after diff.
    """
    return {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": str(user.status),
        "roles": sorted(role.name for role in user.roles),
    }


@router.get(
    "",
    response_model=Page[UserResponse],
    status_code=status.HTTP_200_OK,
    summary="List users",
    description=(
        "Return a paginated slice of users with optional filters and ordering. "
        "`order_by` accepts a column name; prefix with `-` for descending order. Pass "
        "`deleted=true` (requires `users:delete`) for the tombstones inside the restore "
        "window — the set `POST /{user_id}/restore` accepts, subject to its elevation guard."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_users_endpoint(
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_READ))],
    pagination: PaginationDep,
    filters: UserFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: Annotated[
        UserOrderBy,
        Query(description="Column to order by; prefix with `-` for descending."),
    ] = "created_at",
    deleted: DeletedFilterAnyDeleter = False,
) -> Page[UserResponse]:
    """List users.

    `deleted=true` is the restore surface, so it takes the permission that restores
    (`users:delete`) rather than the one that reads the directory. It lists every
    deleter's tombstones, including accounts this caller's elevation level cannot
    restore — the restore itself refuses those, and hiding them would misrepresent the
    directory to the operator who has to ask someone else to act.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:read` permission, or asked for
      `deleted=true` without `users:delete`.
    """
    assert_may_list_deleted(deleted, caller, Permission.USERS_DELETE, entity="users")
    items, total = await list_users(
        db,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted=deleted,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[UserResponse](
        items=[UserResponse.from_user(u) for u in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user",
    description="Fetch one user by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_user_endpoint(
    user_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_READ))],
    db: DbSession,
) -> UserResponse:
    """Fetch one user by id.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:read` permission.
    * **404 Not Found** — no user exists with the given id.
    """
    user = await get_user(db, user_id)
    return UserResponse.from_user(user)


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Update user",
    description=(
        "Partially update a user. Omitted fields are left unchanged. Supplying `role_ids` "
        "replaces the full role set and must contain at least one role id."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("One or more referenced roles do not exist."),
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_user_endpoint(
    user_id: UUID,
    payload: UserUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_UPDATE))],
    db: DbSession,
) -> UserResponse:
    """Partially update a user.

    Only fields explicitly present in the request body are written; omitted
    fields keep their current value. A `role_ids` field replaces the user's
    full role set.

    ### Errors

    * **400 Bad Request** — `role_ids` references an unknown role id.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `users:update`, or `users:manage_admin` when assigning or
      removing the admin role.
    * **404 Not Found** — no user exists with the given id.
    """
    user = await get_user(db, user_id)
    before = _user_snapshot(user)  # capture before update_user mutates the row in place
    data = payload.model_dump(exclude_unset=True)
    data.pop("role_ids", None)
    if payload.role_ids is not None:
        # Pass current roles so dropping an elevated role is gated, not just adding one.
        data["roles"] = await resolve_assignable_roles(db, caller, payload.role_ids, current_roles=user.roles)
    changes = UserUpdateChanges.model_validate(data)
    updated = await update_user(db, user, changes)
    diff_before, diff_after = changed_fields(before, _user_snapshot(updated))
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_UPDATE,
        object_type="user",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return UserResponse.from_user(updated)


@router.post(
    "/{user_id}/restore",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore a user",
    description=(
        "Clear a soft-deleted account's `deleted_at`, returning it with its roles, status and "
        "organization membership as the delete left them. Restorable for a fixed window after the "
        "delete (set per deployment). Gated exactly like the delete, elevation guard included: "
        "restoring an admin requires `users:manage_admin`, since the restore hands every role on "
        "the tombstone back at once. External login links are **not** revived — the next external "
        "login re-links them. Not a re-login: any unexpired token minted before the delete works "
        "again, so force-logout the account first if that is not wanted."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: problem_response(
            "A live account already holds the tombstone's email, or none of its roles is live and active any more."
        ),
    },
)
@transactional
async def restore_user_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_DELETE))],
    db: DbSession,
    settings: SettingsDep,
) -> UserResponse:
    """Restore a soft-deleted user.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:delete` permission, or lacks
      `users:manage_admin` while the tombstone holds the admin role.
    * **404 Not Found** — no restorable user: never deleted, or deleted longer than
      the restore window ago.
    * **409 Conflict** — a live account already holds the tombstone's email, or none
      of the tombstone's roles is live and active any more (restore or reactivate one
      first).
    """
    user = await get_restorable_user(db, user_id, deleted_cutoff=restore_cutoff(settings))
    assert_can_restore_user(caller, user)
    restored = await restore_user(db, user)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_RESTORE,
        object_type="user",
        object_id=user_id,
        after=_user_snapshot(restored),
    )
    return UserResponse.from_user(restored)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete user",
    description="Soft-delete a user (sets `deleted_at`).",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The user is the sole owner of an object; reassign first."),
    },
)
@transactional
async def delete_user_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_DELETE))],
    db: DbSession,
) -> Response:
    """Soft-delete a user.

    Sets `deleted_at` on the row; subsequent reads exclude it. Returns
    `204 No Content` on success.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:delete` permission,
      attempted to delete their own account, or lacks `users:manage_admin`
      while the target holds the admin role.
    * **404 Not Found** — no user exists with the given id.
    * **409 Conflict** — the user is the sole owner of one or more objects
      (e.g. an evaluation group); reassign ownership before deleting them.
    """
    user = await get_user(db, user_id)
    if caller.id == user.id:
        raise ForbiddenError("Cannot delete your own account.")
    assert_can_delete_user(caller, user)
    orphaned = await objects_solely_held_by(db, user.id)
    if orphaned:
        # `protected_role` is non-None for every type `objects_solely_held_by` returns;
        # the walrus just narrows Optional → not-None for `ty`, never filters.
        roles = sorted({role.value for ot, _ in orphaned if (role := OBJECT_ROLE_REGISTRY[ot].protected_role)})
        raise ConflictError(
            f"User is the sole {' / '.join(roles)} of {len(orphaned)} object(s); reassign before deleting them."
        )
    audited = _user_snapshot(user)
    await soft_delete_user(db, user, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_DELETE,
        object_type="user",
        object_id=user_id,
        before=audited,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/force-logout",
    response_model=BulkResponse[ForceLogoutTarget],
    status_code=status.HTTP_200_OK,
    summary="Force-logout users in bulk",
    description=(
        "Revoke all active sessions of up to `BULK_MAX_ROWS` users in one request. Always returns 200 OK "
        "when the envelope is well-formed — a `404` for an unknown user id lands inside `results[].error`. "
        "`dry_run=true` previews the full path and revokes nothing."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_force_logout_endpoint(
    payload: BulkRequest[ForceLogoutTarget],
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_MANAGE_SESSIONS))],
    db: DbSession,
) -> BulkResponse[ForceLogoutTarget]:
    """Force-logout users in bulk with per-row outcomes.

    Side-effects (the Redis revocation + audit entry) are gated on `payload.dry_run`
    so a preview revokes nothing. No `@transactional` — `apply_bulk` owns the
    transaction boundary (commit on success, rollback on dry-run).

    ### Errors

    * **401** — bearer token missing or invalid.
    * **403** — caller lacks `users:manage_sessions`.
    * **422** — duplicate `row_key`, empty `rows`, or over `BULK_MAX_ROWS`.
    """

    async def processor(session: AsyncSession, data: ForceLogoutTarget) -> ForceLogoutTarget:
        user = await get_user(session, data.user_id)
        if not payload.dry_run:
            # Revoke before the audit row: if apply_bulk's commit later fails the user
            # is still logged out (safe) but without a trail — the acceptable direction.
            # A Redis error mid-batch propagates (non-APIError → aborts the whole bulk,
            # 500): earlier rows stay revoked, their audit rows roll back. Same safe
            # direction, wider blast radius — the caller retries.
            await revoke_user_sessions(user.id)
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.USER_FORCE_LOGOUT,
                object_type="user",
                object_id=user.id,
            )
        return ForceLogoutTarget(user_id=user.id)

    return await apply_bulk(db, payload, processor)


@router.post(
    "/{user_id}/force-logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Force-logout a user",
    description="Revoke all of a user's active sessions; their next request and any refresh are rejected.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def force_logout_user_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_MANAGE_SESSIONS))],
    db: DbSession,
) -> Response:
    """Revoke all of a user's active sessions.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:manage_sessions` permission.
    * **404 Not Found** — no user exists with the given id.
    """
    user = await get_user(db, user_id)
    await revoke_user_sessions(user.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_FORCE_LOGOUT,
        object_type="user",
        object_id=user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/status",
    response_model=BulkResponse[UserStatusTarget],
    status_code=status.HTTP_200_OK,
    summary="Change account status in bulk",
    description=(
        "Activate or deactivate up to `BULK_MAX_ROWS` accounts in one request. Each row carries its own target "
        "status — the envelope has no batch-level field, so a UI activating a selection sends the same value on "
        "every row. Always returns 200 OK when the envelope is well-formed; per-row failures (`403` for the "
        "caller's own account or an elevated target, `404` for an unknown id, `409` for an account still "
        "onboarding) land inside `results[].error`. `dry_run=true` previews the full path, revoking no sessions "
        "and writing no audit rows."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_change_user_status_endpoint(
    payload: BulkRequest[UserStatusTarget],
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_UPDATE))],
    db: DbSession,
) -> BulkResponse[UserStatusTarget]:
    """Change account status in bulk with per-row outcomes.

    Session revocation and the audit row are gated on `payload.dry_run` so a preview
    touches neither Redis nor the trail. No `@transactional` — `apply_bulk` owns the
    transaction boundary (commit on success, rollback on dry-run).

    ### Errors

    * **401** — bearer token missing or invalid.
    * **403** — caller lacks `users:update`.
    * **422** — duplicate `row_key`, empty `rows`, an unsettable status, or over `BULK_MAX_ROWS`.
    """

    async def processor(session: AsyncSession, data: UserStatusTarget) -> UserStatusTarget:
        user = await get_user(session, data.user_id)
        if caller.id == user.id:
            raise ForbiddenError("Cannot change your own account status.")
        assert_can_manage_status(caller, user)
        before = _user_snapshot(user)  # capture before change_user_status mutates the row in place
        updated = await change_user_status(session, user, data.status)
        if not payload.dry_run:
            # Same ordering and trade-off as the bulk force-logout: revoke before the audit row,
            # so a failed outer commit leaves the user logged out (safe) rather than the reverse.
            if data.status is UserStatus.INACTIVE:
                await revoke_user_sessions(updated.id)
            diff_before, diff_after = changed_fields(before, _user_snapshot(updated))
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.USER_STATUS_CHANGE,
                object_type="user",
                object_id=updated.id,
                before=diff_before,
                after=diff_after,
            )
        return UserStatusTarget(user_id=updated.id, status=data.status)

    return await apply_bulk(db, payload, processor)


@router.post(
    "/{user_id}/status",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Change a user's account status",
    description=(
        "Activate or deactivate an account. Deactivating also revokes the user's active sessions, so access "
        "stops immediately rather than when their current token expires. Re-sending the status a user already "
        "holds succeeds and changes nothing. Only `active` and `inactive` are settable — an account still "
        "onboarding (`invited` / `pending`) returns 409, because forcing it `active` would leave a passwordless "
        "account that still cannot log in and overwriting it would discard the onboarding state."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The account is still onboarding."),
    },
)
@transactional
async def change_user_status_endpoint(
    user_id: UUID,
    payload: UserStatusChange,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_UPDATE))],
    db: DbSession,
) -> UserResponse:
    """Activate or deactivate a user account.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `users:update`, targeted their own account, or lacks
      `users:manage_admin` while the target holds the admin role.
    * **404 Not Found** — no user exists with the given id.
    * **409 Conflict** — the account is `invited` or `pending`, so its status is not settable.
    """
    user = await get_user(db, user_id)
    if caller.id == user.id:
        # An admin deactivating themselves locks themselves out of the console.
        raise ForbiddenError("Cannot change your own account status.")
    assert_can_manage_status(caller, user)
    before = _user_snapshot(user)  # capture before change_user_status mutates the row in place
    updated = await change_user_status(db, user, payload.status)
    diff_before, diff_after = changed_fields(before, _user_snapshot(updated))
    if payload.status is UserStatus.INACTIVE:
        await revoke_user_sessions(updated.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_STATUS_CHANGE,
        object_type="user",
        object_id=updated.id,
        before=diff_before,
        after=diff_after,
    )
    return UserResponse.from_user(updated)


@router.post(
    "/password-reset",
    response_model=BulkResponse[PasswordResetTarget],
    status_code=status.HTTP_200_OK,
    summary="Send password-reset links in bulk",
    description=(
        f"Mail a password-reset link to up to {MAX_INVITE_ROWS} accounts in one request. Always returns 200 OK "
        "when the envelope is well-formed; per-row failures (`404` for an unknown id, `409` for an account that "
        "is not `active` or has no password to reset) land inside `results[].error`. The same user in more than "
        "one row is rejected (`422`), like a duplicate `row_key` — issuing a token revokes the account's previous "
        "one, so the extra rows would only mail dead links. Emails are dispatched best-effort after the request "
        "has committed, so a row can report `ok` even if its message could not be queued — the caller gets one "
        "in-app notification for the batch when that happens. `dry_run=true` previews the full path and sends "
        "nothing. Like the single-account route, this ignores the self-service reset cooldown and daily cap — "
        "though these sends consume both, so enough of them silently disable the account's own forgot-password "
        "for the rest of the window."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_send_password_resets_endpoint(
    payload: PasswordResetBulkRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_UPDATE))],
    db: DbSession,
) -> BulkResponse[PasswordResetTarget]:
    """Send password-reset links in bulk with per-row outcomes.

    Mail is dispatched after `apply_bulk` commits, not inside the row processor:
    `send_email` enqueues a Celery task, and a task enqueued inside a savepoint that
    later rolls back leaves the broker holding work for a token that no longer exists.
    The single-user endpoint can afford `send_email` directly — one failure there is
    the whole request — but here a broken send must not abort the other rows.

    Each row's mail commits inside the dispatch loop: `send_email`'s 1-second task
    countdown only covers one iteration, and a mail lost to a late commit is invisible
    (the enqueue succeeded, so the row still reports `ok`). Nothing needs to stay atomic
    by then — `apply_bulk` has committed the tokens.

    No `@transactional` — `apply_bulk` owns the transaction boundary.

    ### Errors

    * **401** — bearer token missing or invalid.
    * **403** — caller lacks `users:update`.
    * **422** — duplicate `row_key`, duplicate `user_id`, empty `rows`, or over the row cap.
    """
    specs: list[PasswordResetEmailSpec] = []

    async def processor(session: AsyncSession, data: PasswordResetTarget) -> PasswordResetTarget:
        user = await get_user(session, data.user_id)
        assert_password_reset_allowed(user)
        spec = await issue_password_reset(session, user, triggered_by_admin=True)
        if not payload.dry_run:
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.USER_CREDENTIAL_RESET,
                object_type="user",
                object_id=user.id,
            )
        # Collected last, so a row that failed anywhere above never produces mail.
        specs.append(spec)
        return PasswordResetTarget(user_id=user.id)

    response = await apply_bulk(db, payload, processor)

    if not payload.dry_run:
        batch_key = uuid4()
        lost: list[str] = []
        for spec in specs:
            queued = await send_email_best_effort(
                db,
                spec.template,
                spec.to,
                spec.context,
                secret_context=spec.secret_context,
                requested_by_user_id=caller.id,
                batch_key=batch_key,
            )
            if not queued:
                lost.append(spec.to)
            # Per row: batching this until after the loop outruns the task's countdown.
            await db.commit()
        if lost:
            # `send_email_best_effort` defers the notice to us once a `batch_key` is set, so a
            # broken template writes one row for the batch instead of one per recipient. Own
            # savepoint: the rows `apply_bulk` committed must survive a failure to notify.
            try:
                async with db.begin_nested():
                    await create_notification(
                        db,
                        user_id=caller.id,
                        name="Some password-reset emails could not be sent",
                        description=(
                            f"{len(lost)} of {len(specs)} messages were never queued (first: {lost[0]}). "
                            "Those accounts have no working link — issuing the reset already revoked any "
                            "earlier one — so re-send to them."
                        ),
                    )
            except Exception:
                logger.exception("bulk_password_reset_failure_notification_failed")
        # Lands the notification row — the mails themselves committed per iteration.
        await db.commit()

    return response


@router.post(
    "/{user_id}/password-reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Send a user a password-reset link",
    description=(
        "Mail a fresh password-reset link to an account, revoking any pending reset token. The link goes to the "
        "user's own mailbox — the caller never sees the token and cannot set the password themselves. Unlike the "
        "unauthenticated self-service request, which is always 204 to avoid enumerating accounts, this reports "
        "why it could not send: 404 for an unknown user, 409 for an account that is not `active` or has no "
        "password to reset (it signs in through an identity provider). Not subject to the self-service "
        "cooldown or daily cap either — though these sends consume both, since the mail lands in the same "
        "inbox; enough of them silently disable the account's own forgot-password for the rest of the window."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The account cannot be sent a password reset."),
    },
)
@transactional
async def send_user_password_reset_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_UPDATE))],
    db: DbSession,
) -> Response:
    """Mail a password-reset link to a user.

    No elevation gate, unlike delete / deactivate / invitation-revoke. The link lands
    in the target's own mailbox, so this is not a takeover route; and unlike
    `revoke_platform_invitation` — which `assert_can_manage_invitation` guards because
    it can strip an elevated account's only way in — issuing a reset always mails a
    *replacement* token to that same mailbox, so it cannot deny an admin their
    recovery either. What a non-`users:manage_admin` caller can still do is invalidate
    an elevated account's in-flight reset link and put platform-branded mail in its
    inbox on demand; judged acceptable against the cost of a permission split during
    the concurrent role-map rework.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:update` permission.
    * **404 Not Found** — no user exists with the given id.
    * **409 Conflict** — the account is not `active`, or has no password to reset.
    """
    user = await get_user(db, user_id)
    assert_password_reset_allowed(user)
    spec = await issue_password_reset(db, user, triggered_by_admin=True)
    await send_email(
        db,
        spec.template,
        spec.to,
        spec.context,
        secret_context=spec.secret_context,
        requested_by_user_id=caller.id,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.USER_CREDENTIAL_RESET,
        object_type="user",
        object_id=user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{user_id}/invitation/resend",
    response_model=InvitationResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend a user's invitation",
    description=(
        "Mail a fresh accept link to an account still awaiting activation, revoking any pending token. "
        "Roles are untouched — this re-sends the standing invitation, it does not re-grant it. "
        "Always issues a **platform** invitation: for an account invited only into an evaluation group "
        "this adds one rather than re-sending the group invite, and the mail names the account's global "
        "roles. Returns 409 for an account that is not `invited` (an active, self-registering, or "
        "deactivated user has no invitation to resend)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("User is not awaiting an invitation."),
    },
)
@transactional
async def resend_user_invitation_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_INVITE))],
    db: DbSession,
) -> InvitationResponse:
    """Resend the platform invitation for an invited account.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:invite` permission, or the
      target holds an elevated role the caller may not manage.
    * **404 Not Found** — no user exists with the given id.
    * **409 Conflict** — the account is not in the `invited` state.
    """
    user = await get_user(db, user_id, for_update=True)
    assert_can_manage_invitation(caller, user)
    if user.status is not UserStatus.INVITED:
        raise ConflictError(f"User is {user.status.value}; only an invited account has an invitation to resend.")

    result = await reissue_platform_invitation(db, user, inviter=caller)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.INVITATION_RESEND,
        object_type="user",
        object_id=user.id,
    )
    return InvitationResponse.from_invitation(result.invitation, user)


@router.delete(
    "/{user_id}/invitation",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a user's pending invitation",
    description=(
        "Kill the live **platform** accept link of an account still awaiting activation — an evaluation-group "
        "invite link the account also holds stays live. The invited user row stays (it may already hold roles "
        "handed out at invite time, and a re-invite reuses it) — only the token is revoked. Returns 404 when "
        "the account has no pending platform invitation."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response("User does not exist, or has no pending invitation."),
    },
)
@transactional
async def revoke_user_invitation_endpoint(
    user_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_INVITE))],
    db: DbSession,
) -> Response:
    """Revoke the pending platform invitation of an invited account.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `users:invite` permission, or the
      target holds an elevated role the caller may not manage.
    * **404 Not Found** — no such user, or the account has no pending invitation.
    """
    # The row lock is what serialises this against a concurrent resend: `invitations` carries no
    # uniqueness over (user, pending), so two unlocked callers each see the other's row vanish
    # under the FOR UPDATE re-check and revoke ends up reporting 404 over a live token.
    user = await get_user(db, user_id, for_update=True)
    assert_can_manage_invitation(caller, user)
    await revoke_platform_invitation(db, user)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.INVITATION_REVOKE,
        object_type="user",
        object_id=user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
