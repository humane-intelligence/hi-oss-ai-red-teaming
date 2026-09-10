"""Self-service endpoints on the caller's own account — mounted under `/api/v1/auth/me`."""

from fastapi import APIRouter
from fastapi import Response
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.models import User
from app.core.auth.schemas import MeResponse
from app.core.auth.schemas import MeUpdate
from app.core.auth.schemas import PasswordChange
from app.core.auth.schemas import RoleSummary
from app.core.auth.schemas import SessionUser
from app.core.auth.services.account import change_own_password
from app.core.auth.services.roles import effective_permissions
from app.core.auth.services.users import UserUpdateChanges
from app.core.auth.services.users import get_user
from app.core.auth.services.users import update_user
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.exceptions import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import TERMS_REFUSED
from app.core.openapi import problem_response
from app.core.organizations.schemas import OrganizationBase
from app.core.terms.models import TermsDocument
from app.core.terms.schemas import TermsAccept
from app.core.terms.schemas import TermsDocumentSummary
from app.core.terms.service import accept_terms
from app.core.terms.service import acceptance_required
from app.core.terms.service import get_current_terms
from app.core.terms.service import get_terms

router = APIRouter()

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_NOT_FOUND = problem_response("User does not exist.")
_PASSWORD_REFUSED = problem_response(
    "Current password incorrect, or the new password was rejected by the platform password policy — "
    "length, character classes, common-password denylist, or similarity to the account identity."
)
_NO_PASSWORD = problem_response("The account has no local password; use the password-reset flow instead.")
_STALE_TERMS = problem_response("Those terms are no longer the current version.")


async def _accepted_terms(db: AsyncSession, user: User, current: TermsDocument | None) -> TermsDocument | None:
    """The version the account accepted — `current` when it is up to date, so the common case costs no query."""
    if user.accepted_terms_id is None:
        return None
    if current is not None and current.id == user.accepted_terms_id:
        return current
    return await get_terms(db, user.accepted_terms_id)


def _me_from_row(
    caller: SessionUser,
    user: User,
    current_terms: TermsDocument | None,
    accepted_terms: TermsDocument | None,
) -> MeResponse:
    """Row-backed projection — only the session fields come from the token; the rest reads the DB row."""
    return MeResponse(
        id=caller.id,
        provider=caller.provider,
        email=caller.email,
        email_verified=caller.email_verified,
        first_name=user.first_name,
        last_name=user.last_name,
        has_password=user.password is not None,
        organization=OrganizationBase.from_optional_live(user.organization),
        roles=RoleSummary.from_roles(user.roles),
        permissions=effective_permissions(user),
        consent_terms=user.accepted_terms_id is not None,
        consent_emails=user.consent_emails,
        accepted_terms=TermsDocumentSummary.from_model(accepted_terms) if accepted_terms else None,
        terms_accepted_at=user.terms_accepted_at,
        terms_acceptance_required=acceptance_required(user, current_terms),
    )


@router.get(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current identity",
    description="Return the identity carried by the current bearer token.",
    responses=COMMON_ERROR_RESPONSES | {status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED},
)
async def me(caller: CurrentUserDep, db: DbSession) -> MeResponse:
    """Return the caller's identity plus their live roles and effective permissions.

    Session fields come from the bearer token (the claims `AuthMiddleware`
    decoded). `first_name` / `last_name` / `has_password`, like `roles` and
    `permissions`, are resolved fresh from the database so the FE can refetch
    them after a rename or role change without waiting for the JWT to be
    re-minted; `permissions` is the flattened union across `roles`, the same
    strings the JWT `permissions` claim carries. A session whose `sub` has no
    live DB row falls back to the token: names from the claims, empty `roles`,
    `permissions` from the claim. Only a still-unrevoked token of a
    soft-deleted account reaches that fallback — force-logout, deactivation,
    and a password change all revoke, and the middleware rejects revoked
    tokens before they get here.

    ### Errors

    * **401 Unauthorized** — bearer token is missing, malformed, expired, or
      signed with the wrong secret.
    """
    try:
        user = await get_user(db, caller.id)
    except NotFoundError:
        return MeResponse(
            id=caller.id,
            provider=caller.provider,
            email=caller.email,
            email_verified=caller.email_verified,
            first_name=caller.first_name,
            last_name=caller.last_name,
            has_password=False,
            organization=None,
            roles=[],
            permissions=sorted(caller.permissions),
        )
    current = await get_current_terms(db)
    return _me_from_row(caller, user, current, await _accepted_terms(db, user, current))


@router.patch(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    summary="Update own profile",
    description=(
        "Update the caller's own names and email-consent flag. Omitted fields are left unchanged; "
        "an explicit `null` clears a name and is rejected for `consent_emails`, which backs a "
        "NOT NULL column. Responds with the same projection as `GET /me`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_me_endpoint(payload: MeUpdate, caller: CurrentUserDep, db: DbSession) -> MeResponse:
    """Update the caller's own profile fields.

    Only fields explicitly present in the request body are written; omitted
    fields keep their current value, an explicit `null` clears a name and is
    refused for `consent_emails`. Roles are not reachable here — self-service
    never touches role assignment.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — the account behind the token no longer exists.
    * **422** — an explicit `null` for `consent_emails`; omit the field to leave it unchanged.
    """
    user = await get_user(db, caller.id)
    # Consent is audited alongside the names: a change of what the user agreed to receive has to
    # be answerable later, which a diff of the row alone cannot do.
    before = {"first_name": user.first_name, "last_name": user.last_name, "consent_emails": user.consent_emails}
    changes = UserUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_user(db, user, changes)
    diff_before, diff_after = changed_fields(
        before,
        {
            "first_name": updated.first_name,
            "last_name": updated.last_name,
            "consent_emails": updated.consent_emails,
        },
    )
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
    current = await get_current_terms(db)
    return _me_from_row(caller, updated, current, await _accepted_terms(db, updated, current))


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change own password",
    description=(
        "Verify the current password, set the new one, and revoke every session — including this one; "
        "the caller signs in again with the new password. Passwordless accounts (IdP-only) are refused."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _PASSWORD_REFUSED,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _NO_PASSWORD,
    },
)
@transactional
async def change_my_password_endpoint(payload: PasswordChange, caller: CurrentUserDep, db: DbSession) -> Response:
    """Change the caller's own password.

    Ends every live session on success — the reset flow's rationale: a change whose
    motive may be "someone else has my credentials" must not leave that someone's
    access token working until its own `exp`. The caller signs in again afterwards.

    ### Errors

    * **400 Bad Request** — the current password is wrong (`errors[].type` is
      `current_password_incorrect`), or the new password fails the platform policy:
      `errors[]` points at the `password` field; `errors[].type` is the machine code
      (`password_too_short` / `password_missing_uppercase` / `password_missing_digit` /
      `password_missing_symbol` / `password_too_common` / `password_too_similar`).
    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — the account behind the token no longer exists.
    * **409 Conflict** — the account has no local password (IdP-only, or an OIDC
      activation cleared it); the password-reset flow is the only way to (re)gain one.
    * **422** — the new password is outside the absolute 8-128 bounds every install
      enforces.
    """
    user = await get_user(db, caller.id)
    await change_own_password(db, user, current_password=payload.current_password, new_password=payload.password)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AUTH_CREDENTIAL_CHANGED,
        object_type="user",
        object_id=user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/terms",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    summary="Accept the terms of service",
    description=(
        "Record the caller's acceptance of a specific terms version and respond with the same "
        "projection as `GET /me`, so a client can drop its acceptance gate without a second call. "
        "The id must be the current version: a publish landing between render and submit is refused "
        "rather than accepted on the user's behalf. Re-accepting the version already on the account "
        "changes nothing, timestamp included."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _STALE_TERMS,
    },
)
@transactional
async def accept_terms_endpoint(payload: TermsAccept, caller: CurrentUserDep, db: DbSession) -> MeResponse:
    """Accept a terms-of-service version on the caller's own account.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — the account behind the token no longer exists.
    * **409 Conflict** — `terms_id` is not the current version, or nothing is published;
      refetch the current document and re-render it.
    """
    # Under the row lock: without it two concurrent submits both read "not accepted" and the
    # repeat check below lets each write its own audit entry for the one consent.
    user = await get_user(db, caller.id, for_update=True)
    already_accepted = user.accepted_terms_id == payload.terms_id
    current = await accept_terms(db, user, terms_id=payload.terms_id)
    # A repeat submit is a no-op on the row, so it gets no audit entry either — consent was
    # given once, and a trail that records it twice makes "when did they accept" ambiguous.
    if not already_accepted:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.TERMS_ACCEPT,
            object_type="terms_document",
            object_id=current.id,
            after={"version": current.version},
        )
    return _me_from_row(caller, user, current, current)
