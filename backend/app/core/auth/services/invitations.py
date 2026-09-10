"""Platform invitation lifecycle — issue, lookup, accept.

**Lock order: `users` first, then `invitations`.** Accept, admin resend, admin
revoke, and the `/register` INVITED arm each take `SELECT ... FOR UPDATE` on the
user row before touching invitation rows. Two reasons, and both matter:

* The invitation *set* is what these operations race over ("revoke whatever is
  pending", "re-issue"), and a set has no row to lock. Locking the user row is
  the only mutex that covers it — without it, two callers each take `FOR UPDATE`
  over `status = PENDING`, the second has its row dropped by the re-check when
  the first commits a change, and it then acts on a set it never saw. Concretely:
  revoke reports "no pending invitation" over an accept link resend just minted.
* A consistent order is what keeps these paths from deadlocking each other.
  Postgres aborts one side of an AB-BA with a 500 — on the invitee's accept page,
  if the pair is accept vs. an admin action.

**Known gap:** the re-invite arm of `invite_to_platform` (and its group sibling
`invite_to_group`) still mutates the pending set holding only the invitation-row
locks — `get_user_by_email` takes no user lock — so it can race the four paths
above exactly as described. Closing it means locking the user row there too, and
deciding a row order for bulk batches first (two batches locking overlapping
users in different row order would AB-BA); tracked as a follow-up, don't copy
the pattern.

A new mutation path must follow the same order, and re-read invitation state
*after* both locks: anything read before the user lock may already be stale.
"""

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import held_roles
from app.core.auth.password_policy import PasswordPolicy
from app.core.auth.password_policy import validate_password
from app.core.auth.schemas import SessionUser
from app.core.auth.services.passwords import hash_password
from app.core.auth.services.tokens import generate_raw_token
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.tokens import project_expired
from app.core.auth.services.users import get_user
from app.core.auth.services.users import get_user_by_email
from app.core.auth.services.users import resolve_assignable_roles
from app.core.config import get_settings
from app.core.email import send_email
from app.core.email import send_email_best_effort
from app.core.exceptions import ConflictError
from app.core.exceptions import GoneError
from app.core.exceptions import NotFoundError
from app.core.platform_settings.service import get_platform_settings
from app.core.terms.service import record_signup_consent
from app.core.terms.service import resolve_signup_consent

_TEMPLATE_NAME = "platform_invitation"
_ACCOUNT_ACTIVATED_TEMPLATE = "account_activated"


@dataclass(slots=True, frozen=True)
class InvitationWithToken:
    """Carries the raw token, which only exists in memory long enough to compose the accept URL."""

    invitation: Invitation
    user: User
    raw_token: str


def build_accept_url(raw_token: str) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}/invite/accept?token={raw_token}"


async def _revoke_pending_invitations(
    session: AsyncSession,
    user_id: UUID,
    *,
    object_type: ObjectType | None = None,
    object_id: UUID | None = None,
) -> int:
    """Revoke this user's PENDING invitations *within one scope*; return how many.

    Scope is the `(object_type, object_id)` target: the platform scope
    (`object_type IS NULL`) and each object-scoped invitation are independent, so
    re-issuing one never clobbers the other's live token.

    `SELECT ... FOR UPDATE` serialises concurrent re-issues against the same
    user+scope — without it, two transactions could both load the same PENDING
    rows, revoke them, and each insert a fresh PENDING, ending up with two live
    tokens.
    """
    statement = (
        Invitation.live_select()
        .where(col(Invitation.user_id) == user_id)
        .where(col(Invitation.status) == InvitationStatus.PENDING)
    )
    if object_type is None:
        statement = statement.where(col(Invitation.object_type).is_(None))
    else:
        statement = statement.where(
            col(Invitation.object_type) == object_type,
            col(Invitation.object_id) == object_id,
        )
    result = await session.execute(statement.with_for_update())
    now = datetime.now(UTC)
    rows = result.scalars().all()
    for row in rows:
        row.status = InvitationStatus.REVOKED
        row.revoked_at = now
        session.add(row)
    await session.flush()
    return len(rows)


async def revoke_platform_invitation(session: AsyncSession, user: User) -> None:
    """Revoke the account's pending platform invitation, leaving the account itself alone.

    The invited `User` row stays — it may hold object roles handed out at invite
    time, and re-inviting the same address reuses it. Revoking only kills the live
    platform accept link; a group-scoped token the account holds survives.

    Raises:
        NotFoundError: The account has no pending platform invitation.
    """
    if not await _revoke_pending_invitations(session, user.id):
        raise NotFoundError("User has no pending invitation.")


async def accept_pending_invitations(session: AsyncSession, user_id: UUID) -> None:
    """Sweep the user's still-live PENDING invitations to ACCEPTED on activation.

    Onboarding completes the account once, so every live invitation the user
    holds must be consumed — none may linger `PENDING` against the now-active
    account. Called from both onboarding paths:

    * `accept_invitation` — the user clicked one invite link; this sweeps the
      *siblings* (its own token is already `ACCEPTED` + flushed, so the
      `status == PENDING` filter excludes it). Handles e.g. a platform invite
      alongside a group-scoped one.
    * `verify_email` — a self-signup activates via its email-verification token.
      It has no invitation of its own, but `invite_to_group` may have issued it a
      group-scoped invitation while it was still `PENDING`; this consumes it.

    Only genuinely-live rows are consumed; a lazily-expired or revoked one was
    never valid to accept and is left untouched.

    `FOR UPDATE SKIP LOCKED` — not a plain `FOR UPDATE`. In the `accept_invitation`
    case the caller already holds its own token row lock, so a blanket lock over
    the set would AB-BA deadlock against a concurrent accept of a sibling token
    (each holds the other's row, each wants the full set → PG aborts one with a
    500). Skipping a row a concurrent transaction holds is safe: that transaction
    resolves it itself — its own accept marks it `ACCEPTED`, a reissue's revoke
    marks it `REVOKED`. The only residual is the cosmetic case the sweep targets
    (a skipped row stays `PENDING` if its locker rolls back), strictly better than
    a deadlock.
    """
    result = await session.execute(
        Invitation.live_select()
        .where(col(Invitation.user_id) == user_id)
        .where(col(Invitation.status) == InvitationStatus.PENDING)
        .with_for_update(skip_locked=True),
    )
    now = datetime.now(UTC)
    for invitation in result.scalars().all():
        effective = project_expired(
            invitation.status,
            invitation.expires_at,
            pending=InvitationStatus.PENDING,
            expired=InvitationStatus.EXPIRED,
        )
        if effective is not InvitationStatus.PENDING:
            continue
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now
        session.add(invitation)
    await session.flush()


def _roles_match(user: User, role_ids: list[UUID]) -> bool:
    current = {role.id for role in user.roles if role.deleted_at is None}
    return current == set(role_ids)


async def invite_to_platform(
    session: AsyncSession,
    *,
    email: str,
    role_ids: list[UUID],
    inviter: SessionUser,
    send_side_effects: bool = True,
    batch_key: UUID | None = None,
) -> InvitationWithToken:
    """Issue (or re-issue) a platform invitation; send the mail unless told not to.

    `send_side_effects=False` lets a bulk dry-run caller exercise the persistence
    path without enqueueing mail; the DB write still happens — the outer
    transaction is what bulk rolls back. `batch_key` groups the mails of one bulk
    request so their delivery failures notify the inviter once, not per row.

    Raises:
        BadRequestError: `role_ids` is empty or references unknown roles.
        ForbiddenError: The inviter may not grant one of the requested roles.
        ConflictError: Existing user state forbids the (re-)issue.
    """
    roles = await resolve_assignable_roles(session, inviter, role_ids, current_roles=[])
    role_id_list = [role.id for role in roles]
    existing = await get_user_by_email(session, email)

    if existing is not None:
        if existing.status is UserStatus.ACTIVE:
            raise ConflictError("User already active.")
        if existing.status is UserStatus.PENDING:
            raise ConflictError("User registration in progress.")
        if existing.status is UserStatus.INACTIVE:
            raise ConflictError("User is deactivated. Reactivate via PATCH /users/{id} first.")
        if not _roles_match(existing, role_id_list):
            raise ConflictError(
                "User has different role assignment. Use PATCH /users/{id} to change roles, then reissue invitation.",
            )
        user = existing
        await _revoke_pending_invitations(session, user.id)
    else:
        user = User(email=email, status=UserStatus.INVITED)
        user.roles = list(roles)
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Race against the partial unique index on `users.email`. Mirrors the
            # message `services.users.create_user` raises for the same constraint.
            raise ConflictError("A user with this email already exists.") from exc

    settings = get_settings()
    raw_token = generate_raw_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.invitation_ttl_hours)
    invitation = Invitation(
        user_id=user.id,
        invited_by_user_id=inviter.id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation, attribute_names=["created_at", "updated_at"])

    if send_side_effects:
        await send_email(
            session,
            _TEMPLATE_NAME,
            user.email,
            {
                "inviter_name": inviter.display_name,
                "expires_at": expires_at,
                "role_names": [role.label for role in roles],
            },
            secret_context={"accept_url": build_accept_url(raw_token)},
            requested_by_user_id=inviter.id,
            batch_key=batch_key,
        )

    return InvitationWithToken(invitation=invitation, user=user, raw_token=raw_token)


async def reissue_platform_invitation(
    session: AsyncSession,
    user: User,
    *,
    inviter: SessionUser | None = None,
) -> InvitationWithToken:
    """Re-issue a platform invitation for an existing `INVITED` account.

    Two callers, one mechanic. **Self-service** (`inviter=None`): the
    unauthenticated `/register` path routes an invited account here instead of
    setting a password directly. An email-only request must never bind a credential
    to a pre-existing account: an attacker who knows an invited email could
    otherwise plant a password that the victim's verification click activates —
    against an account carrying preserved invite-time (possibly elevated) roles.
    Re-issuing instead mails a fresh accept link to the account's *own* address,
    where the email owner sets the password through `accept_invitation`.
    **Admin resend** (`inviter` supplied): the same fresh link, attributed — the
    row records the admin and the mail carries their name.

    Either way the account stays `INVITED` until accept and roles are untouched —
    which is why this never re-checks role grantability the way an initial invite
    does; nothing is being granted.
    """
    await _revoke_pending_invitations(session, user.id)
    settings = get_settings()
    raw_token = generate_raw_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.invitation_ttl_hours)
    invitation = Invitation(
        user_id=user.id,
        invited_by_user_id=inviter.id if inviter is not None else None,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation, attribute_names=["created_at", "updated_at"])

    await send_email(
        session,
        _TEMPLATE_NAME,
        user.email,
        {
            "inviter_name": inviter.display_name if inviter is not None else None,
            "expires_at": expires_at,
            "role_names": [role.label for role in user.roles if role.deleted_at is None],
        },
        secret_context={"accept_url": build_accept_url(raw_token)},
        requested_by_user_id=inviter.id if inviter is not None else None,
    )
    return InvitationWithToken(invitation=invitation, user=user, raw_token=raw_token)


async def create_object_invitation(
    session: AsyncSession,
    *,
    user: User,
    inviter_id: UUID,
    object_type: ObjectType,
    object_id: UUID,
) -> InvitationWithToken:
    """Issue a fresh object-scoped invitation token, revoking prior pending ones for the same target.

    Persistence only — the caller composes and sends the domain-specific mail, so
    this generic issuer stays decoupled from any one object type's template. The
    token only onboards the account; the caller pre-assigns the object roles
    (held on the target, inert until the account activates), so this issuer
    carries the `(object_type, object_id)` scope but no role.
    """
    await _revoke_pending_invitations(session, user.id, object_type=object_type, object_id=object_id)
    settings = get_settings()
    raw_token = generate_raw_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.invitation_ttl_hours)
    invitation = Invitation(
        user_id=user.id,
        invited_by_user_id=inviter_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
        object_type=object_type,
        object_id=object_id,
    )
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation, attribute_names=["created_at", "updated_at"])
    return InvitationWithToken(invitation=invitation, user=user, raw_token=raw_token)


@dataclass(slots=True, frozen=True)
class InvitationPreview:
    """Resolved fields needed by `GET /invitations/accept` for FE prefill."""

    email: str
    inviter_name: str | None
    role_names: list[str]
    expires_at: datetime


async def _resolve_invitation_or_raise(session: AsyncSession, raw_token: str) -> Invitation:
    """Look up a live PENDING invitation by token.

    Raises:
        NotFoundError: No live invitation matches the token.
        GoneError: Effective status is not PENDING.
    """
    token_hash = hash_token(raw_token)
    result = await session.execute(
        Invitation.live_select().where(col(Invitation.token_hash) == token_hash),
    )
    invitation = result.scalar_one_or_none()
    if invitation is None:
        raise NotFoundError("Invitation not found.")

    effective = project_expired(
        invitation.status,
        invitation.expires_at,
        pending=InvitationStatus.PENDING,
        expired=InvitationStatus.EXPIRED,
    )
    if effective is not InvitationStatus.PENDING:
        raise GoneError(f"Invitation is {effective.value}.")

    return invitation


async def get_invitation_preview(session: AsyncSession, raw_token: str) -> InvitationPreview:
    """Resolve the token to the public-safe fields FE shows on the accept screen.

    Raises:
        NotFoundError: No live invitation matches the token.
        GoneError: Invitation is `ACCEPTED`, `REVOKED`, or `EXPIRED`.
    """
    invitation = await _resolve_invitation_or_raise(session, raw_token)
    try:
        user = await get_user(session, invitation.user_id)
    except NotFoundError as exc:
        raise GoneError("Invitation user is no longer available.") from exc

    inviter_name: str | None = None
    if invitation.invited_by_user_id is not None:
        inviter = await session.get(User, invitation.invited_by_user_id)
        if inviter is not None:
            inviter_name = inviter.display_name

    # For an object-scoped invite the meaningful roles are the ones pre-assigned
    # on the target (held but inert until accept), not the account's global roles.
    if invitation.object_type is not None and invitation.object_id is not None:
        granted = await held_roles(session, invitation.object_type, invitation.object_id, user.id)
        role_names = [role.label for role in granted]
    else:
        role_names = [role.label for role in user.roles if role.deleted_at is None]

    return InvitationPreview(
        email=user.email,
        inviter_name=inviter_name,
        role_names=role_names,
        expires_at=invitation.expires_at,
    )


async def accept_invitation(
    session: AsyncSession,
    *,
    raw_token: str,
    password: SecretStr,
    first_name: str | None,
    last_name: str | None,
    consent_terms: bool = False,
    consent_emails: bool = False,
    terms_id: UUID | None = None,
) -> User:
    """Set the password, activate the user, mark the invitation accepted.

    Locks `users` before `invitations`, per the module's ordering rule. The token
    lookup is therefore unlocked — it only resolves which account to lock — and
    the invitation is re-read under both locks before anything is written, so a
    revoke or re-issue that lands in that window is seen, not overwritten.

    Concurrent accepts of the same token serialise on the user row: the second
    re-reads the now-`ACCEPTED` status and gets a `GoneError`.

    Activating the account also sweeps any other live invitation the user holds to
    `ACCEPTED` (`accept_pending_invitations`), so a parallel invite in a
    different scope does not linger `PENDING` against the now-active account.

    Raises:
        NotFoundError: Token does not match any live invitation.
        GoneError: Invitation is no longer PENDING, or the account is no longer awaiting onboarding.
        ConsentRequiredError: Terms are published and the form declined them.
        StaleTermsVersionError: The form accepted a version that is no longer current.
    """
    # Ahead of the token lookup: a refusal to accept the terms is about the payload, so it must
    # not depend on — or reveal anything about — the token's state.
    consent_document = await resolve_signup_consent(session, consent_terms=consent_terms, terms_id=terms_id)
    token_hash = hash_token(raw_token)
    found = await session.execute(
        Invitation.live_select().where(col(Invitation.token_hash) == token_hash),
    )
    invitation = found.scalar_one_or_none()
    if invitation is None:
        raise NotFoundError("Invitation not found.")

    locked_user = await session.execute(
        User.live_select().where(col(User.id) == invitation.user_id).with_for_update(),
    )
    user = locked_user.scalar_one_or_none()
    if user is None:
        raise GoneError("Invitation user is no longer available.")

    # Now that the account is pinned, take the invitation and re-read it: everything
    # validated below must be state that cannot change until this transaction ends.
    await session.refresh(invitation, with_for_update=True)
    if invitation.deleted_at is not None:
        raise NotFoundError("Invitation not found.")

    effective = project_expired(
        invitation.status,
        invitation.expires_at,
        pending=InvitationStatus.PENDING,
        expired=InvitationStatus.EXPIRED,
    )
    if effective is not InvitationStatus.PENDING:
        raise GoneError(f"Invitation is {effective.value}.")

    now = datetime.now(UTC)
    if user.status not in (UserStatus.INVITED, UserStatus.PENDING):
        # Only an onboarding account may accept. Re-accepting an ACTIVE (already
        # onboarded via another scope's token — invites are scope-isolated) or
        # INACTIVE (deactivated) account would overwrite its credential, turning
        # a stale token into an unauthenticated password reset.
        raise GoneError("Account is no longer awaiting onboarding.")

    policy = PasswordPolicy.from_settings(await get_platform_settings(session))
    validate_password(password.get_secret_value(), identity=[user.email, first_name, last_name], policy=policy)
    user.password = hash_password(password.get_secret_value())
    user.status = UserStatus.ACTIVE
    user.email_verified_at = now
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    record_signup_consent(user, document=consent_document, consent_emails=consent_emails, now=now)
    session.add(user)

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_at = now
    session.add(invitation)
    await session.flush()
    await session.refresh(user)

    # Activating the account completes onboarding for *every* live invitation the
    # user holds, not just the token clicked — a sibling (e.g. a parallel group
    # invite) must not linger PENDING against the now-active account.
    await accept_pending_invitations(session, user.id)

    # Object-scoped invites pre-assign their roles at issue time (held but inert);
    # activating the account is what makes them effective — nothing to grant here.
    await send_email_best_effort(
        session,
        _ACCOUNT_ACTIVATED_TEMPLATE,
        user.email,
        {"user_name": user.first_name or user.email},
    )
    return user
