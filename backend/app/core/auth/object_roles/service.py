"""Object-role assignment store + access resolution.

The write side (`grant_roles` / `add_member` / `set_member_roles` /
`remove_member`) and the read side (`held_roles`, `list_members`, and
`resolve_object_access` — the gate's input) over the polymorphic
`ObjectRoleAssignment` table. Visibility and ownership of the target object
itself are the owning domain's concern; this layer never loads the object.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectScopeSpec
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import SessionUser
from app.core.auth.services.users import get_roles_by_ids
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.helpers import escape_like
from app.core.soft_delete import with_live


@dataclass(frozen=True, slots=True)
class ObjectMember:
    """One member user with the set of live roles they hold on a single object."""

    user: User
    roles: list[Role]


@dataclass(frozen=True, slots=True)
class ObjectAccessContext:
    """A caller's resolved object-scope authorization — the route gate's input.

    `permissions` is the effective object-scope permission set — the held
    *active* roles' `Role.permissions` when a member, empty otherwise (global JWT
    permissions never grant object-scope authority). `has_super` is the break-glass
    short-circuit — true when the caller carries the type's `super_permission` in
    the JWT, which passes any object check. `is_member` is true when the caller
    holds at least one role on the object — the signal domains use for
    visibility, independent of whether those roles grant any permission.
    """

    object_type: ObjectType
    object_id: UUID
    permissions: frozenset[str]
    is_member: bool
    has_super: bool

    def has(self, permission: Permission) -> bool:
        """Whether the caller effectively holds `permission` on this object."""
        return self.has_super or permission.value in self.permissions


async def held_roles(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user_id: UUID,
) -> list[Role]:
    """Live `Role` rows the user holds on the object (tombstoned assignments/roles excluded)."""
    statement = (
        Role.live_select()
        .join(ObjectRoleAssignment, col(ObjectRoleAssignment.role_id) == col(Role.id))
        .where(
            col(ObjectRoleAssignment.object_type) == object_type,
            col(ObjectRoleAssignment.object_id) == object_id,
            col(ObjectRoleAssignment.user_id) == user_id,
        )
        .options(with_live(ObjectRoleAssignment))
    )
    return list((await session.execute(statement)).scalars().all())


async def holds_permission_anywhere(
    session: AsyncSession, user_id: UUID, object_type: ObjectType, permission: Permission
) -> bool:
    """True when any live role the user holds on an object of ``object_type`` grants ``permission``.

    The coarse companion to `resolve_object_access`, for flat routes that carry no object in
    their path: it answers "does this caller hold the capability *somewhere*", leaving which
    rows they may actually see to the service's own scope. Inactive roles grant nothing, as
    everywhere else object authority is read. Scoped to one ``object_type`` because a
    capability held on one kind of object says nothing about another.
    """
    statement = (
        Role.live_select()
        .join(ObjectRoleAssignment, col(ObjectRoleAssignment.role_id) == col(Role.id))
        .where(
            col(ObjectRoleAssignment.object_type) == object_type,
            col(ObjectRoleAssignment.user_id) == user_id,
            col(Role.is_active).is_(True),
            col(Role.permissions).contains([permission.value]),
        )
        .options(with_live(ObjectRoleAssignment))
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def resolve_object_access(
    session: AsyncSession,
    caller: SessionUser,
    object_type: ObjectType,
    object_id: UUID,
) -> ObjectAccessContext:
    """Resolve `caller`'s effective object-scope permissions on one object.

    Object roles are the only source of object authority: effective permissions
    are the union of the held *active* roles' permissions (empty for a non-member,
    since global JWT permissions never apply at object scope), plus the break-glass
    short-circuit (`has_super`). Does not load or validate the target object; the
    caller checks the object's own existence/visibility.
    """
    spec = OBJECT_ROLE_REGISTRY[object_type]
    roles = await held_roles(session, object_type, object_id, caller.id)
    # Inactive roles grant nothing, but still count as membership (visibility).
    permissions = frozenset(perm for role in roles if role.is_active for perm in role.permissions)
    has_super = spec.super_permission is not None and spec.super_permission.value in caller.permissions
    return ObjectAccessContext(
        object_type=object_type,
        object_id=object_id,
        permissions=permissions,
        is_member=bool(roles),
        has_super=has_super,
    )


async def assert_object_permission(
    session: AsyncSession,
    caller: SessionUser,
    object_type: ObjectType,
    object_id: UUID | None,
    permission: Permission,
) -> None:
    """Refuse unless a role the caller holds on ``object_id`` grants ``permission``.

    The object arm of a gate that accepts the global permission *or* object authority; the
    caller checks the global arm first. Reads the membership-derived set directly instead of
    `ObjectAccessContext.has`, which short-circuits on `has_super`: a break-glass key is itself
    delegable, so honouring it here would let it stand in for a capability the caller was never
    granted. ``object_id`` is None when the request named no object to authorize against — a
    refusal, since the gate must never widen on a missing target.
    """
    if object_id is None:
        raise ForbiddenError(f"Caller lacks the '{permission}' permission.")
    access = await resolve_object_access(session, caller, object_type, object_id)
    if permission.value not in access.permissions:
        raise ForbiddenError(f"Caller lacks the '{permission}' permission.")


def effective_object_permissions(access: ObjectAccessContext) -> frozenset[str]:
    """The caller's effective permission set on the object — for surfacing to clients.

    A member's set is exactly what their held in-group roles grant
    (`access.permissions`). A break-glass caller (`has_super`) gets the object
    type's full vocabulary — every permission any assignable *system* role grants,
    plus the break-glass key — since they may exercise all of it regardless of
    membership. Derived from `OBJECT_ROLE_REGISTRY`, so a permission added to a
    system role flows through with no extra wiring; permissions unique to an
    operator-opted-in custom role are deliberately left out, so an operator can't
    inflate what a break-glass caller reports (their gates pass regardless, since
    `has_super` short-circuits `has`). Per the object-role model these are
    whole-role permission sets, so platform-wide entries a role also carries (e.g.
    `users:invite`) appear here too; consumers gate on the object-relevant keys.
    Mirrors what the per-object gates authorize — never a substitute for them.
    """
    if not access.has_super:
        return access.permissions
    spec = OBJECT_ROLE_REGISTRY[access.object_type]
    permissions = {perm for role in spec.assignable_system_roles for perm in ROLE_PERMISSIONS[role]}
    if spec.super_permission is not None:
        permissions.add(spec.super_permission.value)
    return frozenset(permissions)


async def resolve_assignable_roles(
    session: AsyncSession,
    spec: ObjectScopeSpec,
    role_ids: list[UUID],
) -> list[Role]:
    """Resolve `role_ids` to live, active roles, rejecting any not assignable for this object type.

    Assignability is the role's `is_object_assignable` flag — code-owned for system
    roles (synced from `spec.assignable_system_roles`, so the platform-only admin stays
    out) and an operator opt-in for custom ones. A role must also grant the type's
    `required_permission`, which keeps an opted-in custom role from stranding a
    member inside a group without object-scope read.

    Raises:
        BadRequestError: If a role id is unknown/soft-deleted, inactive, not
            object-assignable, or missing the type's required permission.
    """
    roles = await get_roles_by_ids(session, role_ids)
    invalid = sorted(role.name for role in roles if not role.is_object_assignable)
    if invalid:
        raise BadRequestError(f"Role(s) not assignable for this object: {', '.join(invalid)}.")
    if spec.required_permission is not None:
        lacking = sorted(role.name for role in roles if spec.required_permission.value not in role.permissions)
        if lacking:
            raise BadRequestError(
                f"Role(s) must grant '{spec.required_permission.value}' to be assignable here: {', '.join(lacking)}."
            )
    return roles


async def _live_assignments(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user_id: UUID,
) -> list[ObjectRoleAssignment]:
    """The user's live assignment rows on the object (one per held role)."""
    statement = ObjectRoleAssignment.live_select().where(
        col(ObjectRoleAssignment.object_type) == object_type,
        col(ObjectRoleAssignment.object_id) == object_id,
        col(ObjectRoleAssignment.user_id) == user_id,
    )
    return list((await session.execute(statement)).scalars().all())


async def _protected_role_id(session: AsyncSession, role: SystemRole) -> UUID | None:
    """The live `Role.id` for a canonical role, or `None` if it's absent / soft-deleted."""
    found = (await session.execute(Role.live_select().where(col(Role.name) == role.value))).scalar_one_or_none()
    return found.id if found is not None else None


async def _live_protected_holder_exists(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    role_id: UUID,
    *,
    excluding_user_id: UUID,
) -> bool:
    """Whether a *live* user other than `excluding_user_id` holds `role_id` live on the object.

    A holder backed by a soft-deleted user is a ghost — its assignment outlives the
    user row — and never counts as surviving authority.
    """
    found = (
        await session.execute(
            select(col(ObjectRoleAssignment.id))
            .join(User, col(User.id) == col(ObjectRoleAssignment.user_id))
            .where(
                col(ObjectRoleAssignment.object_type) == object_type,
                col(ObjectRoleAssignment.object_id) == object_id,
                col(ObjectRoleAssignment.role_id) == role_id,
                col(ObjectRoleAssignment.user_id) != excluding_user_id,
                col(ObjectRoleAssignment.deleted_at).is_(None),
                col(User.deleted_at).is_(None),
            )
            .limit(1)
        )
    ).first()
    return found is not None


async def _assert_protected_role_survives(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user_id: UUID,
    dropped: list[ObjectRoleAssignment],
) -> None:
    """Forbid dropping the object's `protected_role` from its sole live holder.

    `dropped` are the assignments this operation is about to tombstone. If one of
    them carries the type's `protected_role` and no *other live* user holds that
    role on the object, the object would be left without that authority — reject
    (the caller must hand the role off first). A no-op when the type declares no
    protected role or the role isn't among the dropped set.

    Locks the protected role's live assignments on the object `FOR UPDATE` first, so
    two concurrent demotions/removals serialize here rather than both reading a
    stale "another holder still exists" and together orphaning the object. A ghost
    holder (live assignment, soft-deleted user) doesn't count — same rule as
    `objects_solely_held_by`.

    Raises:
        ConflictError: If the operation would remove the protected role's last live holder.
    """
    protected = OBJECT_ROLE_REGISTRY[object_type].protected_role
    if protected is None:
        return
    protected_role_id = await _protected_role_id(session, protected)
    if protected_role_id is None or all(a.role_id != protected_role_id for a in dropped):
        return
    await session.execute(
        select(col(ObjectRoleAssignment.id))
        .where(
            col(ObjectRoleAssignment.object_type) == object_type,
            col(ObjectRoleAssignment.object_id) == object_id,
            col(ObjectRoleAssignment.role_id) == protected_role_id,
            col(ObjectRoleAssignment.deleted_at).is_(None),
        )
        .with_for_update()
    )
    if not await _live_protected_holder_exists(
        session, object_type, object_id, protected_role_id, excluding_user_id=user_id
    ):
        raise ConflictError(f"Cannot remove the last '{protected.value}'; assign another holder first.")


async def objects_solely_held_by(
    session: AsyncSession,
    user_id: UUID,
) -> list[tuple[ObjectType, UUID]]:
    """Objects on which `user_id` is the only *live* holder of the type's protected role.

    The user-removal counterpart to `_assert_protected_role_survives`: soft-deleting
    such a user would leave each listed object without its protected authority (an
    evaluation group with no live owner), so the user-delete path consults this to
    block the removal. Only object types declaring a `protected_role` are scanned;
    a holder backed by a soft-deleted user never counts as surviving authority, so a
    lingering ghost assignment can't mask the loss.

    Locks the relevant protected-role assignments `FOR UPDATE`, mirroring the
    member-management guard, so a concurrent deletion of a co-holder on the same
    object serializes here instead of both proceeding and orphaning it.
    """
    sole: list[tuple[ObjectType, UUID]] = []
    for object_type, spec in OBJECT_ROLE_REGISTRY.items():
        if spec.protected_role is None:
            continue
        protected_role_id = await _protected_role_id(session, spec.protected_role)
        if protected_role_id is None:
            continue
        held_ids = set(
            (
                await session.execute(
                    select(col(ObjectRoleAssignment.object_id)).where(
                        col(ObjectRoleAssignment.object_type) == object_type,
                        col(ObjectRoleAssignment.role_id) == protected_role_id,
                        col(ObjectRoleAssignment.user_id) == user_id,
                        col(ObjectRoleAssignment.deleted_at).is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not held_ids:
            continue
        # Lock every live protected-role assignment on those objects so a concurrent
        # deletion of another holder on the same object can't race this check.
        await session.execute(
            select(col(ObjectRoleAssignment.id))
            .where(
                col(ObjectRoleAssignment.object_type) == object_type,
                col(ObjectRoleAssignment.role_id) == protected_role_id,
                col(ObjectRoleAssignment.object_id).in_(held_ids),
                col(ObjectRoleAssignment.deleted_at).is_(None),
            )
            .with_for_update()
        )
        covered = set(
            (
                await session.execute(
                    select(col(ObjectRoleAssignment.object_id))
                    .join(User, col(User.id) == col(ObjectRoleAssignment.user_id))
                    .where(
                        col(ObjectRoleAssignment.object_type) == object_type,
                        col(ObjectRoleAssignment.role_id) == protected_role_id,
                        col(ObjectRoleAssignment.user_id) != user_id,
                        col(ObjectRoleAssignment.deleted_at).is_(None),
                        col(User.deleted_at).is_(None),
                        col(ObjectRoleAssignment.object_id).in_(held_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        sole.extend((object_type, object_id) for object_id in sorted(held_ids - covered, key=str))
    return sole


async def count_members_holding(session: AsyncSession, role_id: UUID) -> int:
    """Count live memberships (a live user on an object) holding `role_id` as an object role.

    Unlike `count_members_solely_holding` this ignores the member's other roles: for
    guarding object-assignability, any holder matters — the role stays live but becomes
    unassignable, so the next edit of that member's role set silently drops it. The
    role's own `is_active` is deliberately not filtered: an inactive role's assignments
    still exist and come back when it is reactivated.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(ObjectRoleAssignment)
            .join(User, col(User.id) == col(ObjectRoleAssignment.user_id))
            .where(
                col(ObjectRoleAssignment.role_id) == role_id,
                col(ObjectRoleAssignment.deleted_at).is_(None),
                col(User.deleted_at).is_(None),
            )
        )
    ) or 0


async def count_members_solely_holding(session: AsyncSession, role_id: UUID) -> int:
    """Count memberships (a live user on an object) whose only live+active object role is `role_id`.

    The role-lifecycle counterpart of `objects_solely_held_by` (per-user,
    protected-role only): deactivating or deleting `role_id` would leave each such
    member with no active role on that object. Distinct from the global-role check
    in `services.roles` — object and global memberships are separate scopes, so a
    member's global role never rescues an object membership here. The join filters
    to live+active roles, so an inactive `role_id` never counts (it already grants
    nothing), a member's inactive other roles don't rescue them, and a ghost holder
    (live assignment, soft-deleted user) is excluded.
    """
    stranded = (
        select(col(ObjectRoleAssignment.user_id))
        .join(User, col(User.id) == col(ObjectRoleAssignment.user_id))
        .join(Role, col(Role.id) == col(ObjectRoleAssignment.role_id))
        .where(
            col(ObjectRoleAssignment.deleted_at).is_(None),
            col(User.deleted_at).is_(None),
            col(Role.deleted_at).is_(None),
            col(Role.is_active).is_(True),
        )
        .group_by(
            col(ObjectRoleAssignment.user_id),
            col(ObjectRoleAssignment.object_type),
            col(ObjectRoleAssignment.object_id),
        )
        .having(and_(func.bool_or(col(ObjectRoleAssignment.role_id) == role_id), func.count() == 1))
        .subquery()
    )
    return (await session.scalar(select(func.count()).select_from(stranded))) or 0


async def grant_roles(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user_id: UUID,
    roles: list[Role],
) -> None:
    """Insert one assignment row per role — the low-level primitive (no conflict check).

    Used where the caller guarantees no existing assignment (e.g. auto-assigning
    the creator the `owner` role on object creation). The member-management
    endpoints go through `add_member` / `set_member_roles` instead.
    """
    for role in roles:
        session.add(
            ObjectRoleAssignment(object_type=object_type, object_id=object_id, user_id=user_id, role_id=role.id)
        )
    await session.flush()


async def list_members(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    *,
    role_id: UUID | None = None,
    search: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[ObjectMember], int]:
    """Return one page of the object's members, grouped per user with their roles.

    Pagination is by *user* (not by assignment row), so a multi-role member is a
    single entry and never straddles a page boundary; `total` is the distinct
    member count. Soft-deleted users are excluded from both the count and the
    page (the assignment outlives the user row), so `total` always matches the
    returned items. `role_id` narrows membership to users holding that role on
    the object — their full live role set is still loaded into each `ObjectMember`.
    `search` narrows to members whose email/first/last name matches the substring
    (case-insensitive; LIKE metacharacters are escaped, so `%` is literal).
    """
    member_ids = (
        select(col(ObjectRoleAssignment.user_id))
        .join(User, col(User.id) == col(ObjectRoleAssignment.user_id))
        .where(
            col(ObjectRoleAssignment.object_type) == object_type,
            col(ObjectRoleAssignment.object_id) == object_id,
            col(ObjectRoleAssignment.deleted_at).is_(None),
            col(User.deleted_at).is_(None),
        )
    )
    if role_id is not None:
        member_ids = member_ids.where(col(ObjectRoleAssignment.role_id) == role_id)
    if search:
        # `search` is a plain kwarg (not a Depends()-bound XFilters model), so escape the
        # LIKE metacharacters here — a raw `%` would otherwise match every member.
        like = f"%{escape_like(search)}%"
        member_ids = member_ids.where(
            or_(
                col(User.email).ilike(like, escape="\\"),
                col(User.first_name).ilike(like, escape="\\"),
                col(User.last_name).ilike(like, escape="\\"),
            )
        )
    member_ids = member_ids.distinct().order_by(col(ObjectRoleAssignment.user_id))
    total = (await session.execute(select(func.count()).select_from(member_ids.subquery()))).scalar_one()
    page_ids = list((await session.execute(member_ids.limit(limit).offset(offset))).scalars().all())
    if not page_ids:
        return [], total

    rows = (
        await session.execute(
            select(col(ObjectRoleAssignment.user_id), Role)
            .join(ObjectRoleAssignment, col(ObjectRoleAssignment.role_id) == col(Role.id))
            .where(
                col(ObjectRoleAssignment.object_type) == object_type,
                col(ObjectRoleAssignment.object_id) == object_id,
                col(ObjectRoleAssignment.deleted_at).is_(None),
                col(ObjectRoleAssignment.user_id).in_(page_ids),
                col(Role.deleted_at).is_(None),
            )
        )
    ).all()
    grouped: dict[UUID, list[Role]] = {user_id: [] for user_id in page_ids}
    for user_id, role in rows:
        grouped[user_id].append(role)

    users = (await session.execute(User.live_select().where(col(User.id).in_(page_ids)))).scalars().all()
    user_by_id = {user.id: user for user in users}
    return [
        ObjectMember(user=user_by_id[user_id], roles=grouped[user_id]) for user_id in page_ids if user_id in user_by_id
    ], total


async def add_member(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user: User,
    roles: list[Role],
) -> ObjectMember:
    """Assign `roles` to `user` on the object, creating one row per role.

    Raises:
        ConflictError: If the user already holds a live role on the object.
    """
    if await _live_assignments(session, object_type, object_id, user.id):
        raise ConflictError("User already has a role on this object.")
    try:
        await grant_roles(session, object_type, object_id, user.id, roles)
    except IntegrityError as exc:
        raise ConflictError("User already has a role on this object.") from exc
    return ObjectMember(user=user, roles=roles)


async def _hidden_roles(session: AsyncSession, role_ids: set[UUID]) -> list[Role]:
    """Those of `role_ids` the API projection hides — the complement of `RoleSummary.from_roles`."""
    if not role_ids:
        return []
    result = await session.execute(
        select(Role).where(
            col(Role.id).in_(role_ids),
            or_(col(Role.is_active).is_(False), col(Role.deleted_at).is_not(None)),
        )
    )
    return list(result.scalars().all())


async def set_member_roles(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user: User,
    roles: list[Role],
    *,
    by_id: UUID,
) -> ObjectMember:
    """Replace the user's role set on the object wholesale.

    Soft-deletes rows whose role left the set and inserts rows for newly added
    roles; unchanged roles keep their existing row (and `created_at`). Roles the
    member projection hides (inactive or tombstoned) are retained rather than
    dropped — the caller never saw them to re-send, and soft-deleting the
    assignment would be unrecoverable even after the role is reactivated. The
    returned member carries them alongside the target set, so a caller diffing
    against `held_roles` doesn't read the retention as a revocation; the response
    shape is unaffected (`RoleSummary.from_roles` filters them out again).

    Enforces the type's `protected_role` invariant: if the new set would drop
    that role from the object's sole live holder, the call is rejected (an
    `evaluation_group` can't be demoted to ownerless — hand the `owner` role off
    first).

    Raises:
        NotFoundError: If the user holds no live role on the object.
        ConflictError: If the new set would remove the protected role's last holder.
    """
    current = await _live_assignments(session, object_type, object_id, user.id)
    if not current:
        raise NotFoundError("User has no role on this object.")

    current_by_role = {assignment.role_id: assignment for assignment in current}
    target_ids = {role.id for role in roles}
    hidden = await _hidden_roles(session, set(current_by_role) - target_ids)
    target_ids |= {role.id for role in hidden}
    dropped = [assignment for role_id, assignment in current_by_role.items() if role_id not in target_ids]
    await _assert_protected_role_survives(session, object_type, object_id, user.id, dropped)
    for assignment in dropped:
        assignment.soft_delete(by_id)
        session.add(assignment)
    new_roles = [role for role in roles if role.id not in current_by_role]
    await grant_roles(session, object_type, object_id, user.id, new_roles)
    return ObjectMember(user=user, roles=[*roles, *hidden])


async def remove_member(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    user_id: UUID,
    *,
    by_id: UUID,
) -> None:
    """Soft-delete every live role the user holds on the object.

    Enforces the type's `protected_role` invariant: removing the object's sole
    live holder of that role is rejected (an `evaluation_group` can't be left
    ownerless — hand the `owner` role off first).

    Raises:
        NotFoundError: If the user holds no live role on the object.
        ConflictError: If the user is the protected role's last holder.
    """
    current = await _live_assignments(session, object_type, object_id, user_id)
    if not current:
        raise NotFoundError("User has no role on this object.")
    await _assert_protected_role_survives(session, object_type, object_id, user_id, current)
    for assignment in current:
        assignment.soft_delete(by_id)
        session.add(assignment)
    await session.flush()


async def soft_delete_object_assignments(
    session: AsyncSession,
    object_type: ObjectType,
    object_id: UUID,
    *,
    by_id: UUID,
) -> None:
    """Tombstone every live assignment on an object — the object-delete hook.

    The polymorphic `object_id` carries no DB foreign key, so cascade is the
    application's job: an owning domain calls this when it soft-deletes the
    target object, satisfying the contract in `models.py`. Unlike `remove_member`
    this spans all users and is a no-op when nothing is assigned.
    """
    current = list(
        (
            await session.execute(
                ObjectRoleAssignment.live_select().where(
                    col(ObjectRoleAssignment.object_type) == object_type,
                    col(ObjectRoleAssignment.object_id) == object_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for assignment in current:
        assignment.soft_delete(by_id)
        session.add(assignment)
    await session.flush()
