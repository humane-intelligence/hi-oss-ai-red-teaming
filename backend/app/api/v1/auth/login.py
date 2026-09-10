"""Email + password login endpoint."""

from fastapi import APIRouter
from fastapi import status

from app.core.auth.schemas import LoginRequest
from app.core.auth.schemas import TokenResponse
from app.core.auth.services.jwt import mint_token_pair
from app.core.auth.services.login import authenticate_credentials
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter()


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Log in with email and password",
    description='Verify credentials and mint a bearer token pair. `provider` claim is `"local"`.',
    responses=COMMON_ERROR_RESPONSES | {status.HTTP_401_UNAUTHORIZED: problem_response("Invalid email or password.")},
)
async def login(payload: LoginRequest, db: DbSession, settings: SettingsDep) -> TokenResponse:
    """Log in with email + password.

    ### Errors

    * **401 Unauthorized** — uniform response for unknown email, wrong
      password, password-less account (IdP-only), or non-ACTIVE status.
      The body never distinguishes between them; the reason is in the
      server logs only.
    """
    user = await authenticate_credentials(
        db,
        email=payload.email,
        password=payload.password.get_secret_value(),
    )
    return mint_token_pair(user, provider="local", settings=settings)
