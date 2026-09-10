"""Evaluation-group invitation endpoints — `/api/v1/evaluation-groups/{group_id}/invitations`.

Invite users by email with in-group roles pre-assigned, through the platform
`BulkRequest` envelope (`/bulk`) — the only path, so a single invitee is a
one-row request. Gated on the object-scope `evaluation_groups:manage_members`
permission (the in-group `owner` or a break-glass admin), exactly like adding an
existing member. The roles are assigned on the group immediately; invitees
without an account yet onboard via the emailed link, after which their
(already-assigned) roles take effect.

The service persists; this route dispatches the mail — only **after**
`apply_bulk` commits, best-effort, so a mail failure can't abort the batch and
failed/aborted rows never produce mail.
"""

from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.email import send_email_best_effort
from app.core.evaluations.dependencies import ManageMembersDep
from app.core.evaluations.schemas import GroupInvitationBulkRequest
from app.core.evaluations.schemas import GroupInvitationCreate
from app.core.evaluations.schemas import GroupInvitationResponse
from app.core.evaluations.services.group_invitations import GroupEmailSpec
from app.core.evaluations.services.group_invitations import invite_to_group
from app.core.logging import get_logger
from app.core.notifications.services.notifications import create_notification
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

logger = get_logger(__name__)

router = APIRouter(prefix="/evaluation-groups/{group_id}/invitations", tags=["evaluation-groups"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks permission to manage this group's members.")
_NOT_FOUND = problem_response("Evaluation group not found, or not visible to the caller.")


@router.post(
    "/bulk",
    response_model=BulkResponse[GroupInvitationResponse],
    status_code=status.HTTP_200_OK,
    summary="Invite users to a group",
    description=(
        f"Invite up to {MAX_INVITE_ROWS} users to the group in one request — the only group-invite path, a "
        "single invitee is a one-row request. Each row is an email with one or more "
        "in-group roles pre-assigned; the roles are assigned on the group immediately, and a row targeting an "
        "active account reports `outcome=assigned` while a new or onboarding one reports `outcome=invited`. "
        "Always returns 200 OK when the envelope is well-formed — per-row failures "
        "(`400` for unknown / non-assignable roles, `409` for deactivated users, a concurrent signup race, "
        "or a re-invite that would drop the group's last `owner`) "
        "land inside `results[].error`. Re-inviting an existing member reconciles their role set; the same "
        "email in more than one row is rejected (`422`), like a duplicate `row_key`. Notification emails are "
        "dispatched best-effort after the request has committed — "
        "a row can report `ok` even if its email could not be queued. `dry_run=true` previews the full "
        "processing path and rolls back instead of committing — no emails are sent."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def bulk_create_group_invitations_endpoint(
    group_id: UUID,
    payload: GroupInvitationBulkRequest,
    context: ManageMembersDep,
    caller: CurrentUserDep,
    db: DbSession,
) -> BulkResponse[GroupInvitationResponse]:
    """Invite users to an evaluation group in bulk with per-row outcomes.

    Group visibility and the `manage_members` permission are checked once at the
    envelope level — every row targets the same group. No `@transactional` —
    `apply_bulk` owns the transaction boundary (commit on success, rollback on
    dry-run).

    Mail is dispatched best-effort after the bulk commit: failed rows never
    produce a spec (the service raises first), an aborted bulk never reaches
    dispatch, and a mail failure can't abort the batch. Known limitation
    (platform-wide, shared with the auth bulk invite): the Celery enqueue
    inside `send_email` precedes the audit-row commit by up to the dispatch
    loop's duration, so a very large batch can outrun the task's 1-second
    countdown and drop mails as `email.task.row_missing` — to be fixed for the
    whole bulk-sending pattern in a separate task.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `evaluation_groups:manage_members` permission for this group.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    * **422 Unprocessable Entity** — duplicate `row_key`, duplicate email, empty `rows`, or over the row cap.
    """
    specs: list[GroupEmailSpec] = []

    async def processor(session: AsyncSession, data: GroupInvitationCreate) -> GroupInvitationResponse:
        result = await invite_to_group(
            session,
            group=context.group,
            inviter=caller,
            email=data.email,
            role_ids=data.role_ids,
        )
        response = GroupInvitationResponse.from_result(result)
        if not payload.dry_run:
            # Rides the row's savepoint (dry-run rolls it back anyway); the guard keeps a
            # previewed batch from flushing audit rows that never commit.
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.INVITATION_CREATE,
                object_type="evaluation_group_invitation",
                object_id=result.user_id,
                after={
                    "email": result.email,
                    "outcome": result.outcome,
                    "roles": sorted(role.name for role in result.roles),
                },
                context={"group_id": str(group_id)},
            )
        # Collected last, so a spec exists only for a fully-built row — a row
        # that fails anywhere above never produces mail.
        specs.append(result.email_spec)
        return response

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
        if lost:
            # One notification for the batch: `send_email_best_effort` defers to us when a
            # `batch_key` is set, precisely so a broken template doesn't write one row per
            # recipient. Own savepoint for the same reason the helper uses one — the rows
            # `apply_bulk` committed must not be undone by the notice about their mail.
            try:
                async with db.begin_nested():
                    await create_notification(
                        db,
                        user_id=caller.id,
                        name="Some invitation emails could not be sent",
                        description=(
                            f"{len(lost)} of {len(specs)} messages were never queued "
                            f"(first: {lost[0]}). The invitations themselves were saved."
                        ),
                    )
            except Exception:
                logger.exception("bulk_invite_failure_notification_failed", group_id=str(group_id))
        # Not redundant: apply_bulk committed only the row data. This lands the
        # OutboundEmail audit rows the dispatch loop flushed — without it they
        # roll back at teardown and the worker drops every bulk mail as
        # `email.task.row_missing`.
        await db.commit()

    return response
