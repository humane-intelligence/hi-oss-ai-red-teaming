"""Platform invitation endpoints.

Accept endpoints are intentionally unauthenticated — the token in the URL is
the invitee's only credential before they finish onboarding.
"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.schemas import InvitationAccept
from app.core.auth.schemas import InvitationBulkRequest
from app.core.auth.schemas import InvitationCreate
from app.core.auth.schemas import InvitationPreview
from app.core.auth.schemas import InvitationResponse
from app.core.auth.schemas import SessionUser
from app.core.auth.services.invitations import accept_invitation
from app.core.auth.services.invitations import get_invitation_preview
from app.core.auth.services.invitations import invite_to_platform
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/invitations", tags=["invitations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Invitation token not found.")
_GONE = problem_response("Invitation is no longer valid (accepted, revoked, or expired).")
_STALE_TERMS = problem_response("The accepted terms version is not the current one.")
_PASSWORD_POLICY = problem_response(
    "Password rejected by the platform password policy — length, character classes, "
    "common-password denylist, or similarity to the account identity."
)


@router.post(
    "/bulk",
    response_model=BulkResponse[InvitationResponse],
    status_code=status.HTTP_200_OK,
    summary="Issue invitations",
    description=(
        f"Issue up to {MAX_INVITE_ROWS} platform invitations in one request — the only invite path, "
        "a single invitee is a one-row request. Creates each target `User` (status=`invited`) if it does "
        "not exist; on re-issue for an already-invited user, revokes pending tokens and emits a fresh one. "
        "Always returns 200 OK when the envelope is well-formed — per-row failures (`409` for users in "
        "incompatible states, `400` for unknown roles, `403` for roles the caller may not grant) land "
        "inside `results[].error`. `dry_run=true` previews the full processing path and rolls back "
        "instead of committing — no emails are sent."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_create_invitations_endpoint(
    payload: InvitationBulkRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.USERS_INVITE))],
    db: DbSession,
) -> BulkResponse[InvitationResponse]:
    """Issue invitations in bulk with per-row outcomes.

    Side-effects (`send_email`) are gated on `payload.dry_run` so a preview
    leaves the outbound mail audit empty. No `@transactional` — `apply_bulk`
    owns the transaction boundary (commit on success, rollback on dry-run).

    ### Errors

    * **401** — bearer token missing or invalid.
    * **403** — caller lacks `users:invite` (per-row, roles the caller may not grant surface in `results[].error`).
    * **422** — duplicate `row_key`, duplicate email, empty `rows`, or more than the row cap.
    """

    # One key for the whole request: the mails go out inside the per-row savepoints, so
    # a provider outage would otherwise notify the caller once per failed recipient.
    batch_key = uuid4()

    async def processor(session: AsyncSession, data: InvitationCreate) -> InvitationResponse:
        result = await invite_to_platform(
            session,
            email=data.email,
            role_ids=data.role_ids,
            inviter=caller,
            send_side_effects=not payload.dry_run,
            batch_key=batch_key,
        )
        return InvitationResponse.from_invitation(result.invitation, result.user)

    return await apply_bulk(db, payload, processor)


@router.get(
    "/accept",
    response_model=InvitationPreview,
    status_code=status.HTTP_200_OK,
    summary="Verify invitation token",
    description=(
        "Resolve an invitation token to the public-safe fields the FE needs to render the accept screen "
        "(email, inviter display name, role names, expiry). Unauthenticated — the token in the query string "
        "is the only auth."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_410_GONE: _GONE,
    },
)
async def get_invitation_accept_endpoint(
    token: Annotated[str, Query(min_length=1, description="Raw invitation token from the accept URL.")],
    db: DbSession,
    response: Response,
) -> InvitationPreview:
    """Verify an invitation token and return prefill data.

    The response carries the invitee's email and the inviter's display name —
    PII that must not be cached by intermediaries or the browser. Setting
    `Cache-Control: no-store` is what enforces that.

    ### Errors

    * **404** — token does not match any live invitation.
    * **410** — invitation accepted, revoked, or expired.
    """
    preview = await get_invitation_preview(db, token)
    response.headers["Cache-Control"] = "no-store"
    return InvitationPreview.from_preview(preview)


@router.post(
    "/accept",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Accept invitation",
    description=(
        "Set the password on the invited account, activate it, mark the invitation accepted, and send a "
        "welcome email. Unauthenticated — the token in the body is the only auth. Returns 204; the FE should "
        "redirect to the login screen."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _PASSWORD_POLICY,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _STALE_TERMS,
        status.HTTP_410_GONE: _GONE,
    },
)
@transactional
async def post_invitation_accept_endpoint(payload: InvitationAccept, request: Request, db: DbSession) -> Response:
    """Accept an invitation and activate the user account.

    ### Errors

    * **400** — password rejected by the platform password policy: shorter than the configured
      minimum, missing a required character class, too common, or too similar to the account
      identity. `errors[]` points at the `password` field; `errors[].type` is the machine code
      (`password_too_short` / `password_missing_uppercase` / `password_missing_digit` /
      `password_missing_symbol` / `password_too_common` / `password_too_similar`). Also returned
      when the platform has published terms of service and `consent_terms` is false, with
      `errors[]` on that field and `errors[].type` `consent_terms_required`.
    * **404** — token does not match any live invitation.
    * **409** — `terms_id` is not the current published version (or was omitted while one is
      published); refetch `GET /api/v1/terms/current` and re-render it before resubmitting.
    * **410** — invitation accepted, revoked, or expired.
    * **422** — password is outside the absolute bounds every install enforces (8-128
      characters); a configured minimum above 8 surfaces as the 400 above.
    """
    user = await accept_invitation(
        db,
        raw_token=payload.token,
        password=payload.password,
        first_name=payload.first_name,
        last_name=payload.last_name,
        consent_terms=payload.consent_terms,
        consent_emails=payload.consent_emails,
        terms_id=payload.terms_id,
    )
    # Best-effort audit subject for AuditAccessMiddleware (never a blocking write here).
    request.state.audit_subject = user.id
    return Response(status_code=status.HTTP_204_NO_CONTENT)
