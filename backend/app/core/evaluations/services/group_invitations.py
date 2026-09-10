"""Invite a single user to an evaluation group with pre-assigned in-group roles.

Bridges the generic object-role layer (`app/core/auth/object_roles/`) and the
platform invitation token flow (`app/core/auth/services/invitations.py`). The
roles are assigned on the group **immediately** in both branches — the
`object_role_assignments` table is the single source of truth — so multi-role
invites work without the invitation carrying any role:

* an **active** account is granted the roles and notified (`outcome=assigned`);
* a **new or still-onboarding** account is granted the roles too, but stays
  inert until it onboards via a group-scoped token (`outcome=invited`); the
  account can't authenticate, so the pending assignments grant nothing until the
  invitee accepts (set a password / sign in) through the shared
  `/auth/invitations/accept` flow.

This service persists only — the notification mail comes back as
`GroupInvitationResult.email_spec` and the **caller dispatches it**. The single
endpoint sends inside its transaction (an `invited` mail is a pre-condition: a
failure rolls the token row back); the bulk endpoint defers dispatch until
after `apply_bulk` commits, so a mail failure can neither abort the batch nor
leak Celery tasks for rolled-back rows.

A user may hold different roles across groups; each group invitation targets one
`(EVALUATION_GROUP, group_id)` independently. Re-inviting the same user
reconciles their role set rather than failing.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import add_member
from app.core.auth.object_roles.service import held_roles
from app.core.auth.object_roles.service import resolve_assignable_roles
from app.core.auth.object_roles.service import set_member_roles
from app.core.auth.schemas import SessionUser
from app.core.auth.services.invitations import build_accept_url
from app.core.auth.services.invitations import create_object_invitation
from app.core.auth.services.roles import get_default_role
from app.core.auth.services.users import get_user_by_email
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import ConflictError

_OBJECT_TYPE = ObjectType.EVALUATION_GROUP
_SCOPE_SPEC = OBJECT_ROLE_REGISTRY[_OBJECT_TYPE]
_INVITATION_TEMPLATE = "evaluation_group_invitation"
_MEMBER_ADDED_TEMPLATE = "evaluation_group_member_added"


@dataclass(frozen=True, slots=True)
class GroupEmailSpec:
    """The notification mail an invite produced; the caller dispatches it.

    For `outcome=invited` the accept URL (the raw token's only escape route)
    rides `secret_context` so it never hits the DB; the spec must therefore be
    sent or dropped within the same request that produced it.
    """

    template: str
    to: str
    context: dict[str, Any]
    secret_context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GroupInvitationResult:
    """Outcome of an invite. `outcome` distinguishes the two branches.

    Attributes:
        outcome: `assigned` — the email belonged to an active account; `invited`
            — an onboarding token was issued. The roles are assigned in both.
        roles: The in-group roles assigned on the group.
        email_spec: The notification mail to dispatch. In the single-invite flow
            an `invited` mail is a pre-condition (send in-transaction, let
            failures propagate) and an `assigned` mail is best-effort; the bulk
            flow deliberately dispatches every spec best-effort after its own
            commit — see the route module docstring for why.
        expires_at: Token expiry for the `invited` branch; `None` when assigned.
    """

    outcome: Literal["assigned", "invited"]
    user_id: UUID
    email: str
    roles: list[Role]
    email_spec: GroupEmailSpec
    expires_at: datetime | None = None


async def _create_invited_user(session: AsyncSession, email: str) -> User:
    default_role = await get_default_role(session)
    user = User(email=email, status=UserStatus.INVITED)
    user.roles = [default_role]
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost a race against the partial unique index on `users.email`.
        raise ConflictError("A user with this email already exists.") from exc
    return user


async def _assign_roles(session: AsyncSession, group_id: UUID, user: User, roles: list[Role], *, by_id: UUID) -> None:
    # Roles live on `object_role_assignments`, not the invitation. Re-inviting an
    # existing member reconciles their set wholesale (`set_member_roles`) rather
    # than failing — the invite means "this user should hold exactly these roles".
    if await held_roles(session, _OBJECT_TYPE, group_id, user.id):
        await set_member_roles(session, _OBJECT_TYPE, group_id, user, roles, by_id=by_id)
    else:
        await add_member(session, _OBJECT_TYPE, group_id, user, roles)


async def invite_to_group(
    session: AsyncSession,
    *,
    group: EvaluationGroup,
    inviter: SessionUser,
    email: str,
    role_ids: list[UUID],
) -> GroupInvitationResult:
    """Invite `email` to `group` with `role_ids` pre-assigned.

    The roles are granted on the group immediately; for a new/onboarding
    account a group-scoped token is issued (inert roles until activation).
    Persistence only — the caller dispatches `result.email_spec`.

    Raises:
        BadRequestError: A role id is unknown or not assignable within a group.
        ConflictError: The user is deactivated.
    """
    roles = await resolve_assignable_roles(session, _SCOPE_SPEC, role_ids)
    existing = await get_user_by_email(session, email)

    if existing is not None and existing.status is UserStatus.INACTIVE:
        raise ConflictError("User is deactivated. Reactivate via PATCH /users/{id} first.")

    if existing is not None and existing.status is UserStatus.ACTIVE:
        await _assign_roles(session, group.id, existing, roles, by_id=inviter.id)
        return GroupInvitationResult(
            outcome="assigned",
            user_id=existing.id,
            email=existing.email,
            roles=roles,
            email_spec=GroupEmailSpec(
                template=_MEMBER_ADDED_TEMPLATE,
                to=existing.email,
                context={
                    "inviter_name": inviter.display_name,
                    "group_title": group.title,
                    "role_names": [role.label for role in roles],
                },
            ),
        )

    # New account, or one still onboarding (invited / pending self-signup): assign
    # the roles now (inert until the account activates) and issue a group-scoped
    # token. The token only onboards the account — the roles are already granted.
    user = existing if existing is not None else await _create_invited_user(session, email)
    await _assign_roles(session, group.id, user, roles, by_id=inviter.id)
    issued = await create_object_invitation(
        session,
        user=user,
        inviter_id=inviter.id,
        object_type=_OBJECT_TYPE,
        object_id=group.id,
    )
    return GroupInvitationResult(
        outcome="invited",
        user_id=user.id,
        email=user.email,
        roles=roles,
        email_spec=GroupEmailSpec(
            template=_INVITATION_TEMPLATE,
            to=user.email,
            context={
                "inviter_name": inviter.display_name,
                "group_title": group.title,
                "expires_at": issued.invitation.expires_at,
                "role_names": [role.label for role in roles],
            },
            secret_context={"accept_url": build_accept_url(issued.raw_token)},
        ),
        expires_at=issued.invitation.expires_at,
    )
