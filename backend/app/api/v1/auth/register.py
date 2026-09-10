"""Self-registration endpoints — public, no bearer token required."""

from fastapi import APIRouter
from fastapi import Request
from fastapi import Response
from fastapi import status

from app.core.auth.schemas import EmailVerifyRequest
from app.core.auth.schemas import RegisterRequest
from app.core.auth.schemas import ResendVerificationRequest
from app.core.auth.services.registration import register_user
from app.core.auth.services.registration import resend_verification
from app.core.auth.services.registration import verify_email
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter()

_NOT_FOUND = problem_response("Verification token not found.")
_GONE = problem_response("Verification token is no longer valid (verified, revoked, or expired).")
_PASSWORD_POLICY = problem_response(
    "Password rejected by the platform password policy — length, character classes, "
    "common-password denylist, or similarity to the account identity; or the published "
    "terms of service were not accepted."
)
_INVITE_ONLY = problem_response("Self-registration is disabled — the platform is in invite-only mode.")
_STALE_TERMS = problem_response("The accepted terms version is not the current one.")


@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Register a new user",
    description=(
        "Start a self-signup. Creates a `PENDING` user with the default `red_teamer` role and sends a "
        "verification email. Always returns 202 — the response does not reveal whether the email is "
        "already associated with an account. Under invite-only mode (platform settings) every request "
        "is refused with a uniform 403 instead."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _PASSWORD_POLICY,
        status.HTTP_403_FORBIDDEN: _INVITE_ONLY,
        status.HTTP_409_CONFLICT: _STALE_TERMS,
    },
)
@transactional
async def register_endpoint(payload: RegisterRequest, db: DbSession) -> Response:
    """Register a new user account.

    ### Errors

    * **400** — password rejected by the platform password policy: shorter than the configured
      minimum, missing a required character class, too common, or too similar to the account
      identity. `errors[]` points at the `password` field; `errors[].type` is the machine code
      (`password_too_short` / `password_missing_uppercase` / `password_missing_digit` /
      `password_missing_symbol` / `password_too_common` / `password_too_similar`). Also returned
      when the platform has published terms of service and `consent_terms` is false, with
      `errors[]` on that field and `errors[].type` `consent_terms_required`.
    * **409** — `terms_id` is not the current published version (or was omitted while one is
      published); refetch `GET /api/v1/terms/current` and re-render it before resubmitting.
    * **403** — invite-only mode is enabled; open self-signup is disabled. Uniform for every
      request (no per-email variance), so the mode adds no enumeration signal.
    * **422** — password is outside the absolute bounds every install enforces (8-128
      characters); a configured minimum above 8 surfaces as the 400 above.

    ### Implementation Notes

    Anti-enumeration — every duplicate-email outcome maps to the same 202
    response. A verification email is sent for genuinely new accounts and for
    re-submissions against a still-pending account; the password on a pending
    account is **not** overwritten, so re-registration cannot hijack a
    half-completed signup. An `invited` account is instead re-issued its
    platform invitation (a fresh accept link to its own address) — the submitted
    password is ignored and the email owner sets the credential via the accept
    link, so an email-only request can never bind a password to a pre-existing
    account. `active` / `inactive` accounts are a silent no-op.

    Residual timing side-channel — the response body is constant, but the work
    isn't: the new-account, pending-resend, and invited-reissue paths run
    several DB writes and an email enqueue (new-account / pending also run
    Argon2), while active / inactive accounts short-circuit after a single
    lookup. The handler does not flatten this; gateway-level rate limiting on
    `/auth/register` is the intended mitigation against enumeration probing.
    """
    await register_user(
        db,
        email=payload.email,
        password=payload.password,
        first_name=payload.first_name,
        last_name=payload.last_name,
        consent_terms=payload.consent_terms,
        consent_emails=payload.consent_emails,
        terms_id=payload.terms_id,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/register/resend",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resend the verification email",
    description=(
        "Re-issue the email-verification link for a pending self-signup, revoking previously "
        "issued links. Always returns 202 — the response does not reveal whether the email is "
        "registered or in which state. Stays open under invite-only mode: it serves signups "
        "already in flight, which the toggle does not govern."
    ),
    responses=COMMON_ERROR_RESPONSES,
)
@transactional
async def resend_verification_endpoint(payload: ResendVerificationRequest, db: DbSession) -> Response:
    """Resend the verification email for a pending registration.

    ### Implementation Notes

    Anti-enumeration — every outcome maps to the same 202. Only a `pending` account
    gets a fresh link; any other state (unknown, `active`, `inactive`, `invited`) is
    a silent no-op — an `invited` account recovers via `POST /auth/register`, which
    re-issues the invitation instead. Gateway-level rate limiting is the intended
    abuse mitigation, as with `/auth/register`.
    """
    await resend_verification(db, email=payload.email)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/register/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Verify email and activate account",
    description=(
        "Consume an email-verification token. Marks the user's email verified, flips the account to "
        "`active`, and sends a welcome email. Returns 204; the FE should redirect to login. "
        "Unauthenticated — the token in the body is the only auth. Stays open under invite-only mode: "
        "the token was already issued, and the toggle governs new signups, not in-flight verifications."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_410_GONE: _GONE,
    },
)
@transactional
async def verify_email_endpoint(payload: EmailVerifyRequest, request: Request, db: DbSession) -> Response:
    """Consume a verification token and activate the account.

    ### Errors

    * **404** — token does not match any live verification.
    * **410** — verification verified, revoked, or expired (incl. user soft-deleted).
    """
    user = await verify_email(db, raw_token=payload.token)
    # Best-effort audit subject for AuditAccessMiddleware (never a blocking write here).
    request.state.audit_subject = user.id
    return Response(status_code=status.HTTP_204_NO_CONTENT)
