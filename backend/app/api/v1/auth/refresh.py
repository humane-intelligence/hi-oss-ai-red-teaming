"""Refresh-token exchange endpoint."""

from fastapi import APIRouter
from fastapi import status

from app.core.auth.schemas import RefreshRequest
from app.core.auth.schemas import TokenResponse
from app.core.auth.services.refresh import refresh_session
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter()


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh an access token",
    description="Exchange a valid refresh token for a new access + refresh token pair.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Invalid or expired refresh token."),
        status.HTTP_503_SERVICE_UNAVAILABLE: problem_response("Session store unavailable; please retry."),
    },
)
async def refresh(payload: RefreshRequest, db: DbSession, settings: SettingsDep) -> TokenResponse:
    """Exchange a refresh token for a new token pair.

    ### Implementation Notes

    The pair is re-minted from the user's *current* roles and status read from
    the database, not from the claims frozen in the refresh token — so a
    deactivated or re-permissioned account is reflected at the next refresh.
    The refresh token is rotated on every call (sliding window); there is no
    server-side refresh-token store yet, so a leaked refresh token stays valid
    until its own `exp`.

    ### Errors

    * **401 Unauthorized** — uniform response for a missing / expired / forged
      refresh token, an access token presented in its place, or a deleted /
      non-active account. The body never distinguishes between them; the reason
      is in the server logs only.
    """
    return await refresh_session(db, refresh_token=payload.refresh_token, settings=settings)
