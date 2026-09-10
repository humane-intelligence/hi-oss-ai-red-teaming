"""Password reset endpoints — unauthenticated; the token in the body is the only auth."""

from fastapi import APIRouter
from fastapi import Response
from fastapi import status

from app.core.auth.schemas import PasswordResetConfirm
from app.core.auth.schemas import PasswordResetRequest
from app.core.auth.services.password_resets import confirm_password_reset
from app.core.auth.services.password_resets import request_password_reset
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/password-resets")

_NOT_FOUND = problem_response("Password reset token not found.")
_GONE = problem_response("Password reset token is no longer valid (used, revoked, or expired).")
_PASSWORD_POLICY = problem_response(
    "Password rejected by the platform password policy — length, character classes, "
    "common-password denylist, or similarity to the account identity."
)


@router.post(
    "/request",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Request password reset",
    description=(
        "Issue a single-use password-reset link to the account email. Always returns 204 regardless of "
        "whether the email is registered — the response shape cannot be used to enumerate accounts. "
        "Repeat requests are throttled per account (an admin-configured cooldown and daily cap); a "
        "throttled request is answered the same way and simply sends no mail."
    ),
    responses=COMMON_ERROR_RESPONSES,
)
@transactional
async def post_password_reset_request_endpoint(payload: PasswordResetRequest, db: DbSession) -> Response:
    """Request a password-reset email."""
    await request_password_reset(db, email=payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Confirm password reset",
    description=(
        "Set a new password using the raw token delivered by the reset email. Also revokes the account's "
        "active sessions, so any device holding a token minted before the reset is signed out rather than "
        "staying in until that token expires."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _PASSWORD_POLICY,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_410_GONE: _GONE,
    },
)
@transactional
async def post_password_reset_confirm_endpoint(payload: PasswordResetConfirm, db: DbSession) -> Response:
    """Set a new password.

    ### Errors

    * **400** — password rejected by the platform password policy: shorter than the configured
      minimum, missing a required character class, too common, or too similar to the account
      identity. `errors[]` points at the `password` field; `errors[].type` is the machine code
      (`password_too_short` / `password_missing_uppercase` / `password_missing_digit` /
      `password_missing_symbol` / `password_too_common` / `password_too_similar`).
    * **404** — token does not match any live password-reset row.
    * **410** — token already used, revoked, expired, or the account is no
      longer eligible (soft-deleted or deactivated).
    * **422** — password is outside the absolute bounds every install enforces (8-128
      characters); a configured minimum above 8 surfaces as the 400 above.
    """
    await confirm_password_reset(db, raw_token=payload.token, password=payload.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
