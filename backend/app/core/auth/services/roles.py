"""The role catalog, deploy-time system-role sync, and operator role CRUD.

`sync_system_roles` is the single writer of the *canonical* roles: it projects
the definitions in `app/core/auth/roles.py` onto their `roles` rows. It runs in
*every* environment — invoked by `scripts.sync_roles` as a deploy step and reused
by `scripts.seed_local` — so, unlike the local-only admin provisioning in
`scripts.seed_local`, it carries no environment guard. Convergence is additive:
non-canonical live roles (e.g. the legacy `participant` seed) are left untouched,
since deleting one could orphan `user_roles` rows.

`create_role` / `update_role` / `delete_role` are the runtime counterpart —
operator-defined (custom) roles managed via the `roles:manage` API, kept off the
canonical name space and the non-delegable permissions.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.auth.object_roles.registry import OBJECT_ASSIGNABLE_SYSTEM_ROLES
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import count_members_holding
from app.core.auth.object_roles.service import count_members_solely_holding
from app.core.auth.roles import DEFAULT_PARTICIPANT_ROLE
from app.core.auth.roles import NON_DEACTIVATABLE_SYSTEM_ROLES
from app.core.auth.roles import NON_DELEGABLE_PERMISSIONS
from app.core.auth.roles import RESERVED_ROLE_NAMES
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.session_revocation import revoke_user_sessions
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row

logger = get_logger(__name__)


async def list_roles(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    include_inactive: bool = False,
    is_object_assignable: bool | None = None,
    deleted: bool = False,
    deleted_cutoff: datetime,
) -> tuple[list[Role], int]:
    """Return one page of live roles ordered by slug `name` — the assignable-role catalog.

    Active-only by default (the assignment picker can't assign inactive roles); the
    management view passes `include_inactive=True` to see deactivated roles too.
    `is_object_assignable` narrows to (or excludes) the roles assignable as in-group
    object roles, so an in-group picker can offer exactly what assignment accepts
    instead of re-deriving that policy client-side; `None` leaves the catalog whole.

    ``deleted`` swaps in the tombstones still inside the restore window, most-recently
    deleted first (this list takes no client ordering). No deleter predicate: the route
    gates the whole view on `roles:manage`, which is also the only tier `restore_role`
    accepts, so every tombstone listed is one the caller may actually restore. The two
    narrowing flags keep their meaning on that branch, which makes `include_inactive`
    load-bearing there: a deactivated role's tombstone is invisible without it, so a
    client offering the deleted view has to pass it (the console does).
    ``deleted_cutoff`` comes from the route, like `get_restorable_role`'s, so the listing
    and the restore cannot disagree on the window.
    """
    statement = deleted_select(Role, deleted_cutoff, deleted_by=None) if deleted else Role.live_select()
    if not include_inactive:
        statement = statement.where(col(Role.is_active).is_(True))
    if is_object_assignable is not None:
        statement = statement.where(col(Role.is_object_assignable).is_(is_object_assignable))
    statement = (
        statement.order_by(col(Role.deleted_at).desc(), col(Role.id)) if deleted else statement.order_by(col(Role.name))
    )
    return await paginate(session, statement, limit=limit, offset=offset)


async def get_role(session: AsyncSession, role_id: UUID, *, for_update: bool = False) -> Role:
    """Fetch one live role by id.

    Raises:
        NotFoundError: If no live role has that id.
    """
    statement = Role.live_select().where(col(Role.id) == role_id)
    if for_update:
        statement = statement.with_for_update()
    role = await session.scalar(statement)
    if role is None:
        raise NotFoundError(f"Role {role_id} does not exist.")
    return role


def effective_permissions(user: User) -> list[str]:
    """Sorted, deduped permission strings from a user's live roles.

    The single union both the JWT mint (`_identity_claims`) and `GET /auth/me`
    project, so the token's `permissions` claim and the REST response can't
    drift. Returns `[]` when `roles` is not eager-loaded — accessing it would
    trigger a lazy load that crashes under an async session — so callers must
    load roles first. Soft-deleted and inactive roles are dropped; output is
    sorted for a deterministic claim/response.
    """
    if "roles" not in user.__dict__:
        return []
    return sorted({p for role in user.roles if role.deleted_at is None and role.is_active for p in role.permissions})


async def get_role_by_name(session: AsyncSession, name: str) -> Role:
    """Resolve a live `Role` by its stable slug `name`.

    Used to look up a canonical role for object-role assignment (e.g. granting
    the creator the `owner` role on object creation). The row exists in any
    environment once `sync_system_roles` has run.

    Raises:
        RuntimeError: If no live role matches `name`. A missing canonical role is
            a broken deploy-time invariant (roles are synced on deploy), not a
            client error — it must surface as a 500, never a 404.
    """
    result = await session.execute(Role.live_select().where(col(Role.name) == name))
    role = result.scalar_one_or_none()
    if role is None:
        raise RuntimeError(f"Canonical role {name!r} not found — system roles are not synced.")
    return role


async def get_default_role(session: AsyncSession) -> Role:
    """Resolve the live, active role auto-assigned to every new user (the `is_default` role).

    Exactly one exists in a synced environment — seeded onto the default
    participant role, then operator-reassignable. Raises `RuntimeError` (→ generic
    500) rather than an `APIError`: a missing default is a broken deploy-time
    invariant, not client-recoverable.
    """
    result = await session.execute(
        Role.live_select().where(col(Role.is_default).is_(True), col(Role.is_active).is_(True))
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise RuntimeError("No active default role found — run `make syncroles` and check the is_default flag.")
    return role


def _validate_custom_permissions(permissions: list[str]) -> None:
    """Reject unknown or non-delegable permission strings for an operator-defined role.

    Every entry must name a real `Permission`; none may be a
    `NON_DELEGABLE_PERMISSIONS` elevation vector — a custom role can't hand out
    the whole-role-set write (`users:update`), role-management, or the
    admin-assignment key. System roles are exempt (their sets come from code, not
    the API), so this guards only the CRUD paths.

    Raises:
        BadRequestError: On any unknown or non-delegable permission.
    """
    unknown = sorted(set(permissions) - {p.value for p in Permission})
    if unknown:
        raise BadRequestError(f"Unknown permission(s): {', '.join(unknown)}.")
    non_delegable = sorted(p.value for p in NON_DELEGABLE_PERMISSIONS if p.value in permissions)
    if non_delegable:
        raise BadRequestError(f"Permission(s) cannot be granted to a custom role: {', '.join(non_delegable)}.")


async def _assert_name_available(session: AsyncSession, name: str) -> None:
    """Reject a name reserved for a system role or already taken by a live role.

    Raises:
        ConflictError: If `name` is a canonical slug or collides with a live role.
    """
    if name in RESERVED_ROLE_NAMES:
        raise ConflictError(f"Role name {name!r} is reserved for a system role.")
    if await session.scalar(Role.live_select().where(col(Role.name) == name)) is not None:
        raise ConflictError(f"A role named {name!r} already exists.")


async def create_role(
    session: AsyncSession,
    *,
    name: str,
    display_name: str,
    description: str | None,
    permissions: list[str],
) -> Role:
    """Create an operator-defined (custom) role.

    Always `is_system=False`, `is_active=True`, `is_default=False`: operators can't
    mint canonical or default roles here. Validates the permission set and the
    name; permissions are stored sorted/deduped. The caller owns the transaction.

    Raises:
        BadRequestError: On an unknown or non-delegable permission.
        ConflictError: On a reserved or already-used name.
    """
    _validate_custom_permissions(permissions)
    await _assert_name_available(session, name)
    role = Role(
        name=name,
        display_name=display_name,
        description=description,
        permissions=sorted(set(permissions)),
        is_system=False,
        is_active=True,
        is_default=False,
    )
    session.add(role)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The pre-check above loses a concurrent create race to the unique index;
        # map the raw IntegrityError to a clean 409 (mirrors `add_member`).
        raise ConflictError(f"A role named {name!r} already exists.") from exc
    return role


async def update_role(
    session: AsyncSession,
    role: Role,
    *,
    display_name: str | None = None,
    description: str | None = None,
    description_provided: bool = False,
    permissions: list[str] | None = None,
    is_object_assignable: bool | None = None,
) -> Role:
    """Edit a custom role's label, description, permission set, and object-assignability.

    System roles are code-owned — their fields are overwritten by
    `sync_system_roles` on every deploy — so any field edit here is rejected;
    only their activation/default flags are mutable (via `set_role_active` /
    `set_default_role`). The slug `name` is immutable once created: there are no
    code references to a custom role's name, but freezing it sidesteps rename
    collisions and dangling references. `None` fields are left unchanged, except
    `description`, which is cleared when `description_provided` says the caller sent it.

    `is_object_assignable` opts the role into being held as an in-group (object) role;
    for system roles it's code-owned and re-converged by `sync_system_roles`, which is
    why only custom roles reach it here.

    Removing a permission revokes every holder's sessions so the lost access takes
    effect immediately; pure additions ride the ≤24h token TTL, so they don't.

    Raises:
        BadRequestError: If `role` is a system role, or a permission is unknown/non-delegable.
        ConflictError: If clearing `is_object_assignable` on the group self-join default
            (which self-join would then be unable to grant) or while any member still
            holds the role on an object, or if the resulting state leaves an
            object-assignable role without the object type's required permission.
    """
    if role.is_system:
        raise BadRequestError(
            f"System role {role.name!r} is code-managed; only its activation and default flags are editable."
        )
    removed: set[str] = set()
    if permissions is not None:
        _validate_custom_permissions(permissions)
        removed = set(role.permissions) - set(permissions)
        role.permissions = sorted(set(permissions))
    if display_name is not None:
        role.display_name = display_name
    # `description` is nullable, so "clear it" is a real request — the route sets
    # `description_provided` from `RoleUpdate.model_fields_set` to tell it from an omission.
    if description_provided:
        role.description = description
    if is_object_assignable is not None:
        if not is_object_assignable and role.is_object_assignable:
            if role.is_participant_default:
                raise ConflictError(
                    f"Role {role.name!r} is the group self-join default; assign a new participant default before "
                    "making it unassignable in groups."
                )
            await _assert_no_group_holders(session, role)
        role.is_object_assignable = is_object_assignable
    # Re-checked after the edits, not only on the flag: dropping the object type's
    # required permission from an already-assignable role reaches the same broken state.
    if role.is_object_assignable:
        _assert_grantable_in_group(role)
    session.add(role)
    await session.flush()
    if removed:
        await _revoke_role_holders_sessions(session, role)
    return role


async def delete_role(session: AsyncSession, role: Role, *, by_id: UUID) -> None:
    """Soft-delete a custom role, enforcing the default- and sole-role guards.

    System roles are code-owned and never deletable. Neither the new-user default
    nor the group participant default can be deleted (reassign it first), nor
    can a role that is some principal's only active role. `effective_permissions`
    already drops tombstoned roles, so a holder's access falls away naturally;
    holders' sessions are revoked so it falls away immediately. The caller owns
    the transaction.

    Raises:
        BadRequestError: If `role` is a system role.
        ConflictError: If `role` is a default (global or participant), or some
            principal's only active role.
    """
    if role.is_system:
        raise BadRequestError(f"System role {role.name!r} cannot be deleted.")
    if role.is_default:
        raise ConflictError(f"The default role {role.name!r} cannot be deleted; assign a new default first.")
    if role.is_participant_default:
        raise ConflictError(
            f"The participant default role {role.name!r} cannot be deleted; assign a new participant default first."
        )
    await _assert_role_not_sole(session, role, action="delete")
    role.soft_delete(by_id)
    session.add(role)
    await session.flush()
    await _revoke_role_holders_sessions(session, role)


async def get_restorable_role(session: AsyncSession, role_id: UUID, *, deleted_cutoff: datetime) -> Role:
    """Fetch the tombstoned role ``role_id`` inside the restore window.

    No deleter predicate: `roles:manage` is the only tier that reaches either the
    deleted listing or the restore, so scoping to the caller's own deletes would hide
    rows from someone allowed to restore them.

    Raises:
        NotFoundError: Never deleted, or deleted longer than the window ago.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(Role, deleted_cutoff, deleted_by=None)
        .where(col(Role.id) == role_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    role = (await session.execute(statement)).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"No restorable role {role_id} was deleted within the restore window.")
    return role


async def restore_role(session: AsyncSession, role: Role) -> Role:
    """Clear ``role``'s tombstone, refusing the code-owned system roles.

    A **system** role tombstone is refused for the same reason `delete_role` refuses to
    create one: those rows are projected from `app/core/auth/roles.py`. Only a direct DB
    write can produce one, and `sync_system_roles` recreates the name rather than reviving
    it, so restoring here would resurrect a row the next deploy has already replaced.

    `is_active` is left as it was — a deactivated role's tombstone restores deactivated,
    granting nothing until it is reactivated. Holders' sessions are *not* revoked: regaining
    a permission is a pure addition, which rides the token TTL exactly as it does in
    `update_role`; only losses revoke.

    The name is the collision that happens in practice (delete `auditor`, create a new
    `auditor`, restore the tombstone) — the `is_default` / `is_participant_default` partial
    indexes can only collide on a tombstone a direct DB write produced, since the delete
    guards both flags.

    Raises:
        BadRequestError: If `role` is a system role.
        ConflictError: If a live role already holds the name.
    """
    if role.is_system:
        raise BadRequestError(f"System role {role.name!r} cannot be restored; it is code-managed.")
    await restore_row(session, role, conflict_message=f"A role named {role.name!r} already exists.")
    return role


def _assert_deactivatable(role: Role) -> None:
    """Reject deactivating a statically protected system role or a current default.

    `admin`/`owner` are protected by name (`NON_DEACTIVATABLE_SYSTEM_ROLES`); the
    new-user default and the group participant default are protected dynamically.
    The DB check constraints also block an inactive default, but a clean
    `ConflictError` beats a raw `IntegrityError`.

    Raises:
        ConflictError: If `role` is admin/owner, the current default, or the current
            participant default.
    """
    if role.is_system and role.name in {r.value for r in NON_DEACTIVATABLE_SYSTEM_ROLES}:
        raise ConflictError(f"System role {role.name!r} cannot be deactivated.")
    if role.is_default:
        raise ConflictError(f"The default role {role.name!r} cannot be deactivated; assign a new default first.")
    if role.is_participant_default:
        raise ConflictError(
            f"The participant default role {role.name!r} cannot be deactivated; assign a new participant default first."
        )


async def _count_users_solely_holding(session: AsyncSession, role_id: UUID) -> int:
    """Count live users whose only live+active global role is `role_id`.

    The global-role arm of the sole-role guard (`user_roles`), mirroring `count_members_solely_holding`
    (the object-role arm): the join filters to live+active roles, so an inactive
    `role_id` never counts and a user's inactive other roles don't rescue them.
    """
    stranded = (
        select(col(UserRole.user_id))
        .join(User, col(User.id) == col(UserRole.user_id))
        .join(Role, col(Role.id) == col(UserRole.role_id))
        .where(
            col(User.deleted_at).is_(None),
            col(Role.deleted_at).is_(None),
            col(Role.is_active).is_(True),
        )
        .group_by(col(UserRole.user_id))
        .having(and_(func.bool_or(col(UserRole.role_id) == role_id), func.count() == 1))
        .subquery()
    )
    return (await session.scalar(select(func.count()).select_from(stranded))) or 0


async def _assert_no_group_holders(session: AsyncSession, role: Role) -> None:
    """Block clearing `is_object_assignable` while any member still holds `role` on an object.

    Un-flagging revokes nothing by itself — object authority reads the held role's
    permissions, not the flag — but it makes the role unassignable, so a later edit of
    that member's role set could no longer preserve it. Stricter than the sole-role guard
    on deactivate/delete: holding another role softens the outcome (the member keeps the
    object) but the grant is gone either way. `set_member_roles` now retains roles the
    projection hides, so this overlaps that retention rather than compensating for it.
    Object arm only — global holdings don't depend on object-assignability.

    Raises:
        ConflictError: If any live member holds `role` on an object.
    """
    members = await count_members_holding(session, role.id)
    if members:
        raise ConflictError(
            f"Cannot make role {role.name!r} unassignable in groups: it is still held by "
            f"{members} group member(s); remove it from them first."
        )


async def _assert_role_not_sole(session: AsyncSession, role: Role, *, action: str) -> None:
    """Block `action` when `role` is some principal's only active role.

    Two independent scopes: any user's only active global role (`user_roles`), or
    any member's only active role on an object (`object_role_assignments`).
    Mirrors the sole-`owner` object guard — reassign the affected principals first.

    Raises:
        ConflictError: If deactivating/deleting would strand any user or member.
    """
    # Same-role concurrent deactivate/delete serializes on the caller's FOR UPDATE
    # of role R. A cross-role skew (a principal's two sole active roles deactivated
    # at once) isn't serialized — admin-rare and recoverable by reassignment.
    users = await _count_users_solely_holding(session, role.id)
    members = await count_members_solely_holding(session, role.id)
    if not (users or members):
        return
    blocked = " and ".join(
        part for part in (f"{users} user(s)" if users else "", f"{members} group member(s)" if members else "") if part
    )
    raise ConflictError(
        f"Cannot {action} role {role.name!r}: it is the only active role for {blocked}; reassign them first."
    )


async def _revoke_role_holders_sessions(session: AsyncSession, role: Role) -> None:
    """Force-logout every live user holding `role` (global assignment).

    Called after a deactivation, deletion, or permission removal so the lost access
    takes effect immediately rather than lingering until the access token expires.
    Object-only holders aren't covered — object permissions resolve live per request,
    so they never go stale in a token.

    Two known costs of revoking here rather than at the route after the commit: a request
    that trips a later guard rolls the DB back with the logouts already applied (holders are
    signed out for a change that didn't happen), and the writes are sequential round-trips
    issued while the role row is `FOR UPDATE`-locked. Both want the pattern the routes use
    for mail — collect the ids here, act on them post-commit — which is a wider refactor of
    three endpoints than this change carries.
    """
    holder_ids = (
        (
            await session.execute(
                select(col(UserRole.user_id))
                .join(User, col(User.id) == col(UserRole.user_id))
                .where(col(UserRole.role_id) == role.id, col(User.deleted_at).is_(None))
            )
        )
        .scalars()
        .all()
    )
    for user_id in holder_ids:
        await revoke_user_sessions(user_id)


async def set_role_active(session: AsyncSession, role: Role, *, is_active: bool) -> Role:
    """Activate or deactivate a role.

    A no-op write (flag already matches) returns early. Deactivation is guarded:
    admin/owner and the current default can't be switched off (`_assert_deactivatable`),
    nor can a role that is some principal's only active role. On a real
    deactivation every holder's sessions are revoked. Reactivation is always
    allowed and revokes nothing.

    Raises:
        ConflictError: If deactivation hits a protected role or would strand a principal.
    """
    if role.is_active == is_active:
        return role
    if not is_active:
        _assert_deactivatable(role)
        await _assert_role_not_sole(session, role, action="deactivate")
    role.is_active = is_active
    session.add(role)
    await session.flush()
    if not is_active:
        await _revoke_role_holders_sessions(session, role)
    return role


def _assert_can_be_default(role: Role) -> None:
    """Reject making a privileged system role a default (global or participant).

    `admin`/`owner` (`NON_DEACTIVATABLE_SYSTEM_ROLES`) carry broad authority;
    auto-granting one to every new user or self-joiner would be an escalation, so
    neither can be set as any default.

    Raises:
        ConflictError: If `role` is admin/owner.
    """
    if role.is_system and role.name in {r.value for r in NON_DEACTIVATABLE_SYSTEM_ROLES}:
        raise ConflictError(f"Role {role.name!r} cannot be made a default role.")


async def set_default_role(session: AsyncSession, role: Role) -> Role:
    """Make `role` the sole new-user default, atomically clearing the previous one.

    The target must be active — an inactive default would break `get_default_role`
    (and violates the DB check constraint) — and can't be a privileged system role
    (`admin`/`owner`, which would auto-elevate every new user). Clears the old
    default's flag before setting the new one so the partial-unique index never sees
    two live defaults. A no-op write (already the default) returns early.

    Raises:
        ConflictError: If `role` is a privileged system role (admin/owner) or inactive.
    """
    _assert_can_be_default(role)
    if not role.is_active:
        raise ConflictError(f"Cannot make inactive role {role.name!r} the default; activate it first.")
    if role.is_default:
        return role
    # Locked: the caller's `FOR UPDATE` covers only the incoming role, so two concurrent
    # promotions of *different* roles would both clear the old default and both set theirs,
    # tripping the partial-unique index as a raw IntegrityError (500) instead of serialising.
    # This and `set_participant_default_role` are the one pair that locks in opposing order
    # (target row, then the current holder of the other flag): promoting each default to the
    # role currently holding the other at the same time deadlocks (40P01 → 500). Admin-rare
    # and resolved by a retry, so the lock order is left as is.
    current = await session.scalar(Role.live_select().where(col(Role.is_default).is_(True)).with_for_update())
    if current is not None:
        current.is_default = False
        session.add(current)
        await session.flush()
    role.is_default = True
    session.add(role)
    await session.flush()
    return role


def _assert_grantable_in_group(role: Role) -> None:
    """Reject a role that in-group assignment would refuse.

    Keeps `is_object_assignable` honest: the flag claims the role can be held in a
    group, so the role must also satisfy what `resolve_assignable_roles` demands.
    Enforced both when a role becomes (or stays) object-assignable and when it is made
    the participant default — self-join grants that one as an object role *without*
    passing through `resolve_assignable_roles`, so it is checked at the moment of
    misconfiguration rather than on a joiner's request.

    One `ObjectType` exists, so consulting its spec is exact; a second would need the
    flag to become per-type rather than requiring every type's permission at once.

    Raises:
        ConflictError: If `role` isn't object-assignable, or lacks the object type's
            required permission.
    """
    spec = OBJECT_ROLE_REGISTRY[ObjectType.EVALUATION_GROUP]
    if not role.is_object_assignable:
        raise ConflictError(f"Role {role.name!r} is not assignable in an evaluation group.")
    if spec.required_permission is not None and spec.required_permission.value not in role.permissions:
        raise ConflictError(
            f"Role {role.name!r} must grant {spec.required_permission.value!r} to be assignable in an evaluation group."
        )


async def set_participant_default_role(session: AsyncSession, role: Role) -> Role:
    """Make `role` the sole group self-join participant default, clearing the previous one.

    Sibling of `set_default_role` for the object-scope. The target must be active
    (an inactive participant default would break `get_participant_default_role` and
    violates the DB check constraint), can't be a privileged system role
    (`admin`/`owner`), and must be grantable as an in-group role — self-join mints an
    object role, so a default that assignment would reject is refused here rather
    than failing every join. Clears the old participant default before setting the new
    one so the partial-unique index never sees two. A no-op write returns early.

    Raises:
        ConflictError: If `role` is a privileged system role (admin/owner), inactive,
            or not grantable in a group.
    """
    _assert_can_be_default(role)
    if not role.is_active:
        raise ConflictError(f"Cannot make inactive role {role.name!r} the participant default; activate it first.")
    _assert_grantable_in_group(role)
    if role.is_participant_default:
        return role
    current = await session.scalar(
        Role.live_select().where(col(Role.is_participant_default).is_(True)).with_for_update()
    )
    if current is not None:
        current.is_participant_default = False
        session.add(current)
        await session.flush()
    role.is_participant_default = True
    session.add(role)
    await session.flush()
    return role


async def get_participant_default_role(session: AsyncSession) -> Role:
    """Resolve the live, active role granted on evaluation-group self-join (the `is_participant_default` role).

    Sibling of `get_default_role` for the object-scope: exactly one exists in a
    synced environment, seeded onto the default participant role and operator-
    reassignable. Raises `RuntimeError` (→ 500) rather than an `APIError`: a
    missing participant default is a broken deploy-time invariant, not client-
    recoverable.
    """
    result = await session.execute(
        Role.live_select().where(col(Role.is_participant_default).is_(True), col(Role.is_active).is_(True))
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise RuntimeError(
            "No active participant default role found — run `make syncroles` and check the is_participant_default flag."
        )
    return role


async def sync_system_roles(session: AsyncSession) -> dict[str, Role]:
    """Upsert the canonical system roles and return a `name -> Role` map.

    Existing live rows have `display_name`, `description`, `permissions`, and
    `is_object_assignable` overwritten from `app/core/auth/roles.py` and the
    object-role registry so the source of truth stays in code. `is_default` and
    `is_participant_default` are seeded only when a row is *created* (and
    `is_active` follows its column default) — never overwritten — so an operator's
    activation or default reassignment survives deploys; object-assignability is
    platform policy, not operator preference, so it re-converges instead.
    Soft-deleted rows are ignored — the partial
    unique index on `roles.name` lets a tombstoned name be re-created. The caller
    owns the transaction; this only flushes.
    """
    names = [role.value for role in SystemRole]
    result = await session.execute(
        Role.live_select().where(col(Role.name).in_(names)),
    )
    existing: dict[str, Role] = {role.name: role for role in result.scalars().all()}

    roles: dict[str, Role] = {}
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for system_role in SystemRole:
        name = system_role.value
        spec = system_role.spec
        display_name = spec.display_name
        description = spec.description
        permissions = sorted(ROLE_PERMISSIONS[system_role])
        is_object_assignable = system_role in OBJECT_ASSIGNABLE_SYSTEM_ROLES

        role = existing.get(name)
        if role is None:
            role = Role(
                name=name,
                display_name=display_name,
                description=description,
                permissions=permissions,
                is_system=True,
                is_default=system_role is DEFAULT_PARTICIPANT_ROLE,
                is_participant_default=system_role is DEFAULT_PARTICIPANT_ROLE,
                is_object_assignable=is_object_assignable,
            )
            session.add(role)
            created.append(name)
        # `is_system` is set unconditionally — for parity-only runs the value
        # is the same, and a legacy non-system row converting to canonical
        # counts as a real change.
        elif (
            role.display_name == display_name
            and role.description == description
            and role.permissions == permissions
            and role.is_system
            and role.is_object_assignable == is_object_assignable
        ):
            unchanged.append(name)
        else:
            role.display_name = display_name
            role.description = description
            role.permissions = permissions
            role.is_system = True
            role.is_object_assignable = is_object_assignable
            updated.append(name)
        roles[name] = role

    await session.flush()
    logger.info(
        "sync_roles.summary",
        created=created,
        updated=updated,
        unchanged=unchanged,
    )
    return roles
