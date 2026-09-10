"""OIDC login + callback endpoints. One handler dispatches all configured IdPs."""

from json import JSONDecodeError
from urllib.parse import urlencode

from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter
from fastapi import Request
from fastapi import status
from fastapi.responses import RedirectResponse
from httpx import HTTPError
from joserfc.errors import JoseError

from app.core.audit.enums import AuditAction
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.oidc import InactiveAccountError
from app.core.auth.services.oidc import InviteOnlyError
from app.core.auth.services.oidc import MissingClaimError
from app.core.auth.services.oidc import UnverifiedEmailError
from app.core.auth.services.oidc import finalize_login
from app.core.auth.services.providers import get_provider
from app.core.auth.services.providers import provider_names
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/oidc", tags=["oidc"])

logger = get_logger(__name__)

# Where the SPA picks the handshake back up, appended to `frontend_base_url`.
_SPA_CALLBACK_PATH = "/auth/callback"


def _spa_redirect(base_url: str, params: dict[str, str]) -> RedirectResponse:
    """302 to the SPA's callback route, carrying `params` in the URL **fragment**.

    A fragment, not a query string: browsers never put it on the wire, so the
    tokens stay out of the FE server's access log and out of the `Referer` of
    whatever the SPA loads next. `no-store` is defense in depth on top of
    that — the `Location` header itself carries a live token pair on success.
    """
    return RedirectResponse(
        url=f"{base_url.rstrip('/')}{_SPA_CALLBACK_PATH}#{urlencode(params)}",
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )


def _build_redirect_uri(provider: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/auth/oidc/{provider}/callback"


@router.get(
    "/providers",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="List configured OIDC providers",
    description="Names of configured OIDC providers — clients use these to build a login UI.",
    responses=COMMON_ERROR_RESPONSES,
)
async def list_providers() -> list[str]:
    return provider_names()


@router.get(
    "/{provider}/login",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    summary="Begin OIDC login",
    description=(
        "Redirect to the IdP's authorize endpoint. authlib stashes `state` + `nonce` "
        "on the short-lived `oidc_state` cookie so the callback can validate the "
        "round-trip; that cookie is unrelated to the app's Bearer session. A network "
        "failure reaching the IdP redirects to the SPA with `#error=oauth_error` "
        "instead of a 500, same as the callback."
    ),
    responses=COMMON_ERROR_RESPONSES | {status.HTTP_404_NOT_FOUND: problem_response("Unknown OIDC provider.")},
)
async def login(provider: str, request: Request, settings: SettingsDep) -> RedirectResponse:
    client = get_provider(provider)
    redirect_uri = _build_redirect_uri(provider, settings.oauth_redirect_base_url)
    try:
        return await client.authorize_redirect(request, redirect_uri)
    except (HTTPError, JSONDecodeError, RuntimeError) as exc:
        # `authorize_redirect` resolves `server_metadata_url` lazily on first use — a real
        # outbound GET to the IdP's discovery endpoint, cached only once it succeeds. Left
        # unguarded, a network failure here is a raw JSON 500 from a top-level browser
        # navigation with no route back into the SPA — the same failure the callback's own
        # `HTTPError` handling exists to prevent on the other leg of the handshake. A 2xx/4xx
        # response with a non-JSON body (a captive portal, a TLS-intercepting proxy, an IdP
        # maintenance page) isn't an `HTTPError` either — authlib's `raise_for_status()` only
        # covers >= 500, so `resp.json()` raises `JSONDecodeError` past it; and a discovery
        # document missing `authorization_endpoint` raises a bare `RuntimeError` from
        # `create_authorization_url`. Neither is a network failure, but both are the same
        # "the IdP is broken" shape one is, so they redirect the same way.
        logger.info("auth.oidc.login_failed", provider=provider, reason="oauth_error", detail=str(exc))
        return _spa_redirect(settings.frontend_base_url, {"error": "oauth_error"})


@router.get(
    "/{provider}/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    summary="Complete OIDC login",
    description=(
        "Exchange the IdP code for tokens, resolve (or provision) the local user, and "
        "redirect the browser to the SPA with the bearer token pair in the URL fragment "
        "(`#access_token=…&refresh_token=…&expires_in=…`). A first login for an unknown "
        "IdP identity links it to the account owning the claimed email, or creates one. "
        "Every in-flow failure redirects to the same SPA route with `#error=<code>` "
        "instead — the browser lands here by navigation, so a JSON error body would "
        "strand the user on a dead page."
    ),
    name="auth_callback",
    responses=COMMON_ERROR_RESPONSES | {status.HTTP_404_NOT_FOUND: problem_response("Unknown OIDC provider.")},
)
@transactional
async def callback(
    provider: str,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
) -> RedirectResponse:
    """Complete an OIDC login and hand the session back to the SPA.

    ### Implementation Notes

    Errors are reported as `#error=<code>` on the same redirect, not as RFC 7807
    bodies: `access_denied` (consent refused), `invalid_claims`, `email_unverified`,
    `account_inactive`, `invite_only` (no account owns the email and the platform
    refuses self-serve signup), `login_conflict` (a concurrent first login for the
    same email lost a race a retry resolves), `oauth_error` (a network failure
    reaching the IdP, or an `OAuthError` with no code of its own), plus any code
    the IdP itself returned.
    An unknown or unconfigured `{provider}`, which can only come from a
    hand-built URL rather than from the flow, still answers **404** with a
    problem body.
    """
    client = get_provider(provider)
    frontend_base_url = settings.frontend_base_url

    # `finally` so the short-lived `oidc_state` cookie is dropped symmetrically
    # on success and on error — otherwise it would linger until max_age=600.
    try:
        try:
            token = await client.authorize_access_token(request)
        except (OAuthError, HTTPError, JoseError, JSONDecodeError) as exc:
            # A network failure reaching the IdP's token endpoint (timeout, connection
            # refused) isn't an `OAuthError` — authlib's async OAuth2 client subclasses
            # httpx.AsyncClient directly, so that raises httpx's own exception instead.
            # A bad id_token (forged signature, expired `exp`, nonce mismatch) isn't
            # either — `authorize_access_token` parses and validates it inline via
            # `joserfc`, a third, unrelated exception hierarchy. Nor is a 2xx/4xx response
            # with a non-JSON body (`parse_response_token`'s `raise_for_status()` only
            # covers >= 500, same blind spot as the login leg) — `resp.json()` raises
            # `JSONDecodeError` past it. All four redirect like every other in-flow
            # failure rather than leaking a raw 500.
            if isinstance(exc, OAuthError):
                code = exc.error or "oauth_error"
            elif isinstance(exc, JoseError):
                code = "invalid_claims"
            else:
                code = "oauth_error"
            logger.info("auth.oidc.handshake_failed", provider=provider, code=code, detail=str(exc))
            return _spa_redirect(frontend_base_url, {"error": code})

        claims = token.get("userinfo") or {}
        try:
            tokens = await finalize_login(db, provider=provider, claims=dict(claims), settings=settings)
        except (MissingClaimError, UnverifiedEmailError, InviteOnlyError) as exc:
            # One arm for the refusals that never resolve an account — no subject to
            # attribute, so (unlike `account_inactive`) they set no audit state.
            codes: dict[type[Exception], str] = {
                MissingClaimError: "invalid_claims",
                UnverifiedEmailError: "email_unverified",
                InviteOnlyError: "invite_only",
            }
            code = codes[type(exc)]
            logger.info("auth.oidc.login_failed", provider=provider, reason=code, detail=str(exc))
            return _spa_redirect(frontend_base_url, {"error": code})
        except InactiveAccountError as exc:
            logger.info("auth.oidc.login_failed", provider=provider, reason="account_inactive", detail=str(exc))
            # `AuditAccessMiddleware` reads this unconditionally for this route — every
            # outcome that reaches here is a 302, so (unlike `_LOGIN_ROUTE`) status code
            # carries no signal. `exc.user_id` is never unset at this raise site — refusal
            # only happens after resolution.
            request.state.audit_action = AuditAction.AUTH_LOGIN_FAILED
            request.state.audit_subject = exc.user_id
            return _spa_redirect(frontend_base_url, {"error": "account_inactive"})
        except ConflictError as exc:
            # `finalize_login` already resolves the ordinary concurrent-first-login race by
            # adopting the winner's row; this only fires if that recovery itself lost a race
            # (vanishingly unlikely) — still redirect rather than surface a raw 409.
            logger.info("auth.oidc.login_failed", provider=provider, reason="login_conflict", detail=str(exc))
            return _spa_redirect(frontend_base_url, {"error": "login_conflict"})
    finally:
        request.session.clear()

    # Decode rather than thread the id back through `finalize_login`'s return
    # type — that return value is also what a plain password login mints, and
    # widening it here would ripple into an unrelated contract.
    session_user = decode_session_jwt(
        tokens.access_token,
        secret=settings.session_jwt_secret.get_secret_value(),
        algorithm=settings.session_jwt_algorithm,
    )
    request.state.audit_action = AuditAction.AUTH_LOGIN
    request.state.audit_subject = session_user.id
    return _spa_redirect(
        frontend_base_url,
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_in": str(tokens.expires_in),
        },
    )
