"""User CRUD service — pure async functions over an `AsyncSession`.

Routers stay thin: they raise `APIError` subclasses for known failure modes
and let the error handler in `app.core.error_handlers` translate them into
RFC 7807 responses. Soft-deleted rows are filtered per-statement via the
`Model.live_*` factories on `BaseModel` (see
[app/core/soft_delete.py](../../soft_delete.py)); reads that also eager-load
roles attach `with_live(Role)` so soft-deleted role rows drop out of the
loaded collection. Reads always eagerly load `User.roles` — every caller
projecting a `User` through `UserResponse` needs that collection available
without triggering a lazy load on an async session.
"""

from datetime import UTC
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import SecretStr
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.base import ExecutableOption
from sqlmodel import col

from app.core.auth.filters import UserFilters
from app.core.auth.filters import UserOrderBy
from app.core.auth.models import Invitation
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import Role
from app.core.auth.models import SettableUserStatus
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.auth.models import UserStatus
from app.core.auth.roles import ELEVATED_ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import SessionUser
from app.core.auth.services.passwords import hash_password
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.organizations.models import Organization
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


class UserUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `UserUpdate` fail loudly at construction
    instead of silently landing in the DB. Build from
    `payload.model_dump(exclude_unset=True)` so `model_fields_set` separates
    "omitted" from "explicit None"; substitute the request's `role_ids` for
    pre-resolved `Role` rows so the primitive doesn't re-query.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    first_name: str | None = None
    last_name: str | None = None
    roles: list[Role] | None = None
    # Self-service only — the admin `UserUpdate` deliberately omits it, so no admin can flip
    # someone else's consent.
    consent_emails: bool | None = None


async def get_roles_by_ids(session: AsyncSession, role_ids: list[UUID]) -> list[Role]:
    """Resolve `role_ids` to live, active `Role` rows for assignment.

    The shared chokepoint for both global and object-role assignment. Soft-deleted
    roles read as missing (`Role.live_select()` filters them); an inactive role
    resolves but can't be assigned. Both yield `BadRequestError`. Result order
    matches `role_ids` for deterministic persistence.

    Raises:
        BadRequestError: If any id is unknown/soft-deleted, or resolves to an
            inactive role.
    """
    unique_ids = list(dict.fromkeys(role_ids))
    result = await session.execute(Role.live_select().where(col(Role.id).in_(unique_ids)))
    by_id = {role.id: role for role in result.scalars().all()}
    missing = [rid for rid in unique_ids if rid not in by_id]
    if missing:
        formatted = ", ".join(str(rid) for rid in missing)
        raise BadRequestError(f"Unknown role(s): {formatted}.")
    inactive = sorted(role.name for role in by_id.values() if not role.is_active)
    if inactive:
        raise BadRequestError(f"Cannot assign inactive role(s): {', '.join(inactive)}.")
    return [by_id[rid] for rid in unique_ids]


def assert_can_assign_roles(
    caller: SessionUser,
    roles: list[Role],
    current_roles: list[Role],
) -> None:
    """Reject the operation unless `caller` may change every elevated role it touches.

    The endpoint's own permission gate (`require_permission`) governs *who can
    manage users at all*; this adds the finer rule that a few roles need an
    extra elevated permission to grant or strip. `ELEVATED_ROLE_PERMISSIONS`
    maps each elevated role to that permission (today only admin, via
    `users:manage_admin`). Enforced wherever a request changes a user's roles —
    invitations, user create, and user update — and on user delete when the
    target holds the elevated role.

    Symmetric, and gated on the *full assigned set* rather than the delta:
    `roles` is the complete set the target will hold (PATCH replaces wholesale),
    so re-sending an already-held elevated role still counts as assigning it.
    `current_roles` (the target's roles before the change) extends this to
    *removing* an elevated role the caller can't grant — otherwise a manager
    lacking `users:manage_admin` could silently demote an admin. Pass `[]` on
    create / invitation, where there is no prior set: making the parameter
    required (no default) forces call sites to choose explicitly, so an update
    path can't silently skip revocation gating by forgetting to forward
    `user.roles`. An empty list reads unambiguously as "nothing to revoke"
    because the domain guarantees every live user holds at least one role.

    Args:
        caller: The authenticated user performing the change.
        roles: The live roles the target will hold after the change.
        current_roles: The target's live roles before the change; `[]` on
            create / invitation where there is no prior set. Only elevated roles
            being *removed* are gated; dropping a non-elevated role is always
            allowed.

    Raises:
        ForbiddenError: The caller lacks the elevated permission for a role it
            is assigning or removing.
    """
    elevated_names = {role.value for role in ELEVATED_ROLE_PERMISSIONS}
    requested = {role.name for role in roles} & elevated_names
    held = {role.name for role in current_roles} & elevated_names

    assigning = sorted(
        name for name in requested if ELEVATED_ROLE_PERMISSIONS[SystemRole(name)] not in caller.permissions
    )
    if assigning:
        raise ForbiddenError(f"Caller cannot assign role(s): {', '.join(assigning)}.")

    revoking = sorted(
        name for name in held - requested if ELEVATED_ROLE_PERMISSIONS[SystemRole(name)] not in caller.permissions
    )
    if revoking:
        raise ForbiddenError(f"Caller cannot revoke role(s): {', '.join(revoking)}.")


def _blocked_elevated_roles(caller: SessionUser, user: User) -> list[str]:
    """Elevated roles `user` holds that `caller` lacks the permission to manage."""
    elevated_names = {role.value for role in ELEVATED_ROLE_PERMISSIONS}
    held = {role.name for role in user.roles} & elevated_names
    return sorted(name for name in held if ELEVATED_ROLE_PERMISSIONS[SystemRole(name)] not in caller.permissions)


def assert_can_delete_user(caller: SessionUser, user: User) -> None:
    """Reject the delete unless `caller` may revoke every elevated role `user` holds.

    Deleting a user effectively strips all their roles, so the same elevation
    rule that gates explicit revocation gates deletion too — without this, a
    manager lacking `users:manage_admin` could drop an admin by deleting them
    rather than demoting them.
    """
    blocked = _blocked_elevated_roles(caller, user)
    if blocked:
        raise ForbiddenError(f"Caller cannot delete a user holding role(s): {', '.join(blocked)}.")


def assert_can_restore_user(caller: SessionUser, user: User) -> None:
    """Reject the restore unless `caller` may grant every elevated role `user` holds.

    Restoring hands every role on the tombstone back at once, so it is the delete read
    backwards and takes the same elevation rule: without this, a manager lacking
    `users:manage_admin` could mint an admin by restoring one, which is the escalation
    `assert_can_delete_user` blocks in the other direction.
    """
    blocked = _blocked_elevated_roles(caller, user)
    if blocked:
        raise ForbiddenError(f"Caller cannot restore a user holding role(s): {', '.join(blocked)}.")


def assert_can_manage_invitation(caller: SessionUser, user: User) -> None:
    """Reject resend/revoke unless `caller` may manage every elevated role `user` holds.

    `users:invite` alone is held by roles that cannot touch an admin (`owner`
    today), so without this an owner could kill a pending admin's only accept
    link, or re-mint it under their own name — acting on an elevated account
    they may neither grant nor revoke roles on.
    """
    blocked = _blocked_elevated_roles(caller, user)
    if blocked:
        raise ForbiddenError(f"Caller cannot manage the invitation of a user holding role(s): {', '.join(blocked)}.")


def assert_can_manage_status(caller: SessionUser, user: User) -> None:
    """Reject the status change unless `caller` may manage every elevated role `user` holds.

    Deactivating an account strips all its access, so it belongs to the same class
    as deleting or demoting it — without this, a caller lacking `users:manage_admin`
    could neutralize an admin by flipping them to `inactive` instead.
    """
    blocked = _blocked_elevated_roles(caller, user)
    if blocked:
        raise ForbiddenError(f"Caller cannot change the status of a user holding role(s): {', '.join(blocked)}.")


async def resolve_assignable_roles(
    session: AsyncSession,
    caller: SessionUser,
    role_ids: list[UUID],
    current_roles: list[Role],
) -> list[Role]:
    """Resolve `role_ids` to live roles, asserting `caller` may apply the change.

    The single chokepoint for request-driven role assignment — invitations,
    user create, and user update all route through it, so the elevation rule
    (`assert_can_assign_roles`) can't be silently skipped on one path. The
    low-level `create_user` / `update_user` primitives deliberately stay
    auth-free (they also back unauthenticated provisioning: seeding, fixtures),
    so the gate lives here at the request boundary rather than inside them.

    Pass `current_roles` (the target's roles before the change) on update paths
    so the gate also rejects *removing* an elevated role the caller can't grant;
    pass `[]` on create / invitation, where there is no prior set. The parameter
    is required (no default) so an update path can't silently skip revocation
    gating by forgetting to forward `user.roles`.

    Raises:
        BadRequestError: `role_ids` references unknown / soft-deleted / inactive roles.
        ForbiddenError: `caller` lacks the elevated permission for a role it is
            assigning or removing.
    """
    roles = await get_roles_by_ids(session, role_ids)
    assert_can_assign_roles(caller, roles, current_roles)
    return roles


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    roles: list[Role],
    first_name: str | None = None,
    last_name: str | None = None,
    password: SecretStr | None = None,
    email_verified: bool = False,
    status: UserStatus = UserStatus.INVITED,
) -> User:
    """Create and persist a new `User` with at least one role assignment.

    The boolean `email_verified` flag is a convenience for callers — it's
    translated to an `email_verified_at` timestamp here (stamped at call
    time when true, left null otherwise) so the persistence layer only ever
    sees the canonical timestamp form.

    Args:
        session: Async DB session bound to the request.
        email: Primary email; must be unique among live (non-soft-deleted) users,
            enforced by a partial unique index on `deleted_at IS NULL`.
        roles: Live `Role` rows to assign; must be non-empty. Callers that hold
            ids resolve them through `get_roles_by_ids` (or
            `resolve_assignable_roles` on request paths) first.
        first_name: Optional given name.
        last_name: Optional family name.
        password: Optional plaintext password; Argon2id-hashed before storage.
        email_verified: Whether the email is pre-verified (admin provisioning).
        status: Initial lifecycle state — defaults to `invited`.

    Returns:
        The persisted `User` with `roles` eagerly loaded.

    Raises:
        BadRequestError: If `roles` is empty.
        ConflictError: If the email is already taken.
    """
    if not roles:
        raise BadRequestError("A user must be created with at least one role.")
    user = User(
        email=email,
        first_name=first_name,
        last_name=last_name,
        password=hash_password(password.get_secret_value()) if password is not None else None,
        email_verified_at=datetime.now(UTC) if email_verified else None,
        status=status,
    )
    user.roles = roles
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A user with this email already exists.") from exc
    await session.refresh(user, attribute_names=["created_at", "updated_at"])
    return user


def user_projection_options() -> tuple[ExecutableOption, ...]:
    """Loader options eager-loading everything `UserResponse.from_user` projects.

    Roles, the owning organization, and the platform invitations, each filtered to
    live rows via `with_live`, so a `User` handed to the response layer never
    triggers a detached lazy-load on the async session. Every query that returns a
    `User` for projection must apply these — keeping the set in one place stops a
    future projection change from silently missing a call site.
    """
    return (
        selectinload(User.roles),  # ty: ignore[invalid-argument-type]
        with_live(Role),
        selectinload(User.organization),  # ty: ignore[invalid-argument-type]
        with_live(Organization),
        selectinload(User.invitations),  # ty: ignore[invalid-argument-type]
        with_live(Invitation),
    )


async def get_user(
    session: AsyncSession,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> User:
    """Fetch one live (non-soft-deleted) user by id, with roles eagerly loaded.

    Args:
        session: Async DB session bound to the request.
        user_id: Primary key of the user to fetch.
        for_update: Append `FOR UPDATE` to the primary query so the row lock
            is taken with the read. Use when the caller will mutate this user
            (or rows that need per-user serialisation) later in the same
            transaction. Does not lock rows loaded via `selectinload`. That load
            also sets `populate_existing`, so an instance an earlier unlocked read
            left in the identity map is overwritten with the locked state rather
            than handed back stale — otherwise a post-lock re-check would read
            pre-lock values and the row lock would protect nothing.

    Raises:
        NotFoundError: If no live row matches ``user_id``.
    """
    statement = User.live_select().options(*user_projection_options()).where(col(User.id) == user_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(statement)
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Fetch one live user by email, with roles eagerly loaded.

    Returns ``None`` rather than raising — different callers translate
    "no such user" into different errors (e.g. login: 401, invitation:
    create fresh row).
    """
    statement = User.live_select().options(*user_projection_options()).where(col(User.email) == email)
    result = await session.execute(statement)
    return result.scalar_one_or_none()


def _apply_user_filters(statement: Select[tuple[User]], filters: UserFilters) -> Select[tuple[User]]:
    """Append a `WHERE` clause for every supplied filter, leave omitted ones alone."""
    if filters.status is not None:
        statement = statement.where(col(User.status) == filters.status)
    if filters.email is not None:
        statement = statement.where(col(User.email).ilike(f"%{filters.email}%", escape="\\"))
    if filters.first_name is not None:
        statement = statement.where(col(User.first_name).ilike(f"%{filters.first_name}%", escape="\\"))
    if filters.last_name is not None:
        statement = statement.where(col(User.last_name).ilike(f"%{filters.last_name}%", escape="\\"))
    if filters.search is not None:
        like = f"%{filters.search}%"
        statement = statement.where(
            or_(
                col(User.email).ilike(like, escape="\\"),
                col(User.first_name).ilike(like, escape="\\"),
                col(User.last_name).ilike(like, escape="\\"),
            )
        )
    if filters.role_id is not None:
        statement = statement.where(
            col(User.id).in_(select(col(UserRole.user_id)).where(col(UserRole.role_id) == filters.role_id))
        )
    return statement


async def list_users(
    session: AsyncSession,
    *,
    filters: UserFilters,
    order_by: UserOrderBy,
    limit: int,
    offset: int,
    deleted: bool = False,
    deleted_cutoff: datetime,
) -> tuple[list[User], int]:
    """Return one page of live users matching ``filters``, ordered by ``order_by``.

    Roles are eagerly loaded so each item can be projected through
    `UserResponse.from_user` without further DB I/O. `with_live(Role)`
    filters tombstoned role rows out of the loaded `User.roles` collection.

    ``deleted`` swaps in the tombstones still inside the restore window, keeping
    ``order_by`` as given (pass `-deleted_at` for newest-first). No deleter predicate:
    the route gates the whole view on `users:delete`, and the elevation guard — not the
    identity of the deleter — decides which of those tombstones the caller may restore.
    ``deleted_cutoff`` comes from the route, like `get_restorable_user`'s, so the listing
    and the restore cannot disagree on the window.
    """
    statement = (deleted_select(User, deleted_cutoff, deleted_by=None) if deleted else User.live_select()).options(
        *user_projection_options()
    )
    statement = _apply_user_filters(statement, filters)
    statement = apply_order_by(statement, User, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def update_user(session: AsyncSession, user: User, changes: UserUpdateChanges) -> User:
    """Apply ``changes`` to ``user`` and persist them.

    Writes only fields in `changes.model_fields_set` — omitted fields stay,
    explicit `None` clears. A non-None ``changes.roles`` replaces the role set
    (caller has already resolved + authorized the new set), except that roles the
    API projection hides are retained — see below.

    Raises:
        ConflictError: Reserved for future unique-constraint paths.
    """
    set_fields = changes.model_fields_set
    if "roles" in set_fields and changes.roles is not None:
        # `RoleSummary.from_roles` omits inactive/tombstoned roles, so a full-set PATCH
        # (the contract) would destroy a grant the caller never saw and cannot re-send
        # (`get_roles_by_ids` refuses inactive ids). They grant nothing meanwhile, so
        # keeping them costs nothing and reactivating the role restores the access.
        assigned = {role.id for role in changes.roles}
        hidden = [
            role
            for role in user.roles
            if role.id not in assigned and (not role.is_active or role.deleted_at is not None)
        ]
        user.roles = [*changes.roles, *hidden]
    if "first_name" in set_fields:
        user.first_name = changes.first_name
    if "last_name" in set_fields:
        user.last_name = changes.last_name
    if "consent_emails" in set_fields and changes.consent_emails is not None:
        user.consent_emails = changes.consent_emails
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("User update violates a uniqueness constraint.") from exc
    await session.refresh(user, attribute_names=["updated_at"])
    return user


async def change_user_status(session: AsyncSession, user: User, status: SettableUserStatus) -> User:
    """Switch ``user`` between `active` and `inactive`; no-op when already there.

    Only the two settable statuses are a valid *source* too — an `invited` or
    `pending` account is mid-onboarding, and `SettableUserStatus` explains why
    neither is a legal target.

    Deactivation is not complete until the caller also revokes the user's sessions:
    `SessionUser` is built from JWT claims alone, so an `inactive` account keeps
    working until its access token expires. That revocation stays with the caller
    rather than living here because a bulk dry-run must preview the transition
    without touching Redis, which no rollback can undo.

    Raises:
        ConflictError: The account's current status is not `active` or `inactive`.
    """
    if user.status is status:
        return user
    if user.status not in (UserStatus.ACTIVE, UserStatus.INACTIVE):
        raise ConflictError(
            f"User is {user.status.value}; only an active or inactive account can be activated or deactivated."
        )
    user.status = status
    session.add(user)
    await session.flush()
    await session.refresh(user, attribute_names=["updated_at"])
    return user


async def soft_delete_user(session: AsyncSession, user: User, *, by_id: UUID) -> User:
    """Mark ``user`` as soft-deleted by stamping `deleted_at`.

    Idempotent in effect — a second call overwrites `deleted_at` with a
    newer timestamp rather than raising; callers that need "already deleted"
    detection should check the original value before calling. Cascades to the
    user's `provider_identities`: without this, a live identity row keeps
    resolving to the tombstoned account (`_get_identity` wins before the
    email-link path ever runs), permanently blocking that IdP subject from
    ever reaching a recreated account for the same email.
    """
    user.soft_delete(by_id)
    session.add(user)
    await session.execute(
        ProviderIdentity.live_update()
        .where(col(ProviderIdentity.user_id) == user.id)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    await session.flush()
    await session.refresh(user)
    return user


async def get_restorable_user(session: AsyncSession, user_id: UUID, *, deleted_cutoff: datetime) -> User:
    """Fetch the tombstoned user ``user_id`` inside the restore window, roles loaded.

    No deleter predicate: `users:delete` reaches every tombstone in the listing, and it
    is the elevation guard (`assert_can_restore_user`) that decides which of them the
    caller may actually revive — scoping by deleter would hide an admin's tombstone from
    the one caller entitled to bring it back.

    Roles come eagerly loaded because both the guard and the response projection read
    them; `FOR UPDATE` locks only the `users` row, not the `selectinload`ed collections.

    Raises:
        NotFoundError: Never deleted, or deleted longer than the window ago.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(User, deleted_cutoff, deleted_by=None)
        .options(*user_projection_options())
        .where(col(User.id) == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No restorable user {user_id} was deleted within the restore window.")
    return user


async def restore_user(session: AsyncSession, user: User) -> User:
    """Clear ``user``'s tombstone, leaving the account exactly as the delete found it.

    Deliberately shallow in one place: the `provider_identities` the delete cascaded to
    stay tombstoned. Their unique index is partial, so the next external login re-links
    the same `(provider, subject)` as a fresh row — reviving them here would only
    duplicate that work, and it would resurrect a link the operator may have deleted the
    account to break.

    `status` is left as it was, so a deleted-while-inactive account comes back inactive.
    Sessions are not touched either way: the delete revokes nothing (a tombstoned user
    fails the liveness lookup on every request, which is what cuts access), so an
    unexpired token minted before it works again — the delete undone, not a re-login
    granted. Use the force-logout endpoint when that is not wanted.

    Raises:
        ConflictError: If a live user already holds the email, or none of the
            tombstone's roles is live and active any more — the sole-role guards on
            role delete and deactivate skip tombstoned holders, so this is where
            "every live user holds at least one active role" gets re-established.
    """
    if not any(role.is_active for role in user.roles):
        raise ConflictError(
            f"Cannot restore {user.email!r}: none of its roles is live and active. Restore or reactivate one first."
        )
    await restore_row(session, user, conflict_message=f"A user with email {user.email!r} already exists.")
    await session.refresh(user, attribute_names=["updated_at"])
    return user
