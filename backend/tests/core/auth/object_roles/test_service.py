"""Integration tests for the object-role assignment store + access resolution.

Exercises the generic service against a real DB with `ObjectType.EVALUATION_GROUP`
and a synthetic `object_id` (the table has no FK to the target object).
"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.schemas import ObjectMemberResponse
from app.core.auth.object_roles.service import add_member
from app.core.auth.object_roles.service import assert_object_permission
from app.core.auth.object_roles.service import effective_object_permissions
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import held_roles
from app.core.auth.object_roles.service import holds_permission_anywhere
from app.core.auth.object_roles.service import list_members
from app.core.auth.object_roles.service import objects_solely_held_by
from app.core.auth.object_roles.service import remove_member
from app.core.auth.object_roles.service import resolve_assignable_roles
from app.core.auth.object_roles.service import resolve_object_access
from app.core.auth.object_roles.service import set_member_roles
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import SessionUser
from app.core.auth.services.users import create_user
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError

pytestmark = pytest.mark.integration

_GROUP = ObjectType.EVALUATION_GROUP
_SPEC = OBJECT_ROLE_REGISTRY[_GROUP]


async def _user(db: AsyncSession) -> User:
    role = Role(name=f"r-{uuid4().hex[:8]}", description="test", permissions=[])
    db.add(role)
    await db.flush()
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


def _session_user(user: User, permissions: set[str]) -> SessionUser:
    return SessionUser(
        id=user.id,
        email=user.email,
        email_verified=True,
        first_name=None,
        last_name=None,
        provider="local",
        permissions=frozenset(permissions),
    )


def _names(roles: list[Role]) -> set[str]:
    return {role.name for role in roles}


async def test_grant_roles_then_held_roles_returns_assignment(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    user = await _user(db_session)
    object_id = uuid4()

    await grant_roles(db_session, _GROUP, object_id, user.id, [system_roles["owner"]])

    assert _names(await held_roles(db_session, _GROUP, object_id, user.id)) == {"owner"}


async def test_add_member_rejects_existing_member(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"]])

    with pytest.raises(ConflictError):
        await add_member(db_session, _GROUP, object_id, user, [system_roles["viewer"]])


async def test_set_member_roles_replaces_the_set(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["annotator"]])

    await set_member_roles(
        db_session, _GROUP, object_id, user, [system_roles["red_teamer"], system_roles["viewer"]], by_id=uuid4()
    )

    assert _names(await held_roles(db_session, _GROUP, object_id, user.id)) == {"red_teamer", "viewer"}


async def test_set_member_roles_retains_a_held_inactive_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Hidden from the member projection, so a wholesale replace must not soft-delete the
    # assignment — reactivating the role would not bring it back.
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["annotator"]])
    system_roles["annotator"].is_active = False
    db_session.add(system_roles["annotator"])
    await db_session.flush()

    member = await set_member_roles(db_session, _GROUP, object_id, user, [system_roles["viewer"]], by_id=uuid4())

    assert _names(await held_roles(db_session, _GROUP, object_id, user.id)) == {"viewer", "annotator"}
    # Returned too, so a caller diffing this against `held_roles` doesn't read the
    # retention as a revocation.
    assert _names(member.roles) == {"viewer", "annotator"}


async def test_set_member_roles_on_non_member_raises(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    user = await _user(db_session)

    with pytest.raises(NotFoundError):
        await set_member_roles(db_session, _GROUP, uuid4(), user, [system_roles["viewer"]], by_id=uuid4())


async def test_remove_member_clears_all_roles(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"], system_roles["viewer"]])

    await remove_member(db_session, _GROUP, object_id, user.id, by_id=uuid4())

    assert await held_roles(db_session, _GROUP, object_id, user.id) == []


async def test_remove_then_readd_is_allowed(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    # The partial unique index excludes tombstoned rows, so re-adding works.
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"]])
    await remove_member(db_session, _GROUP, object_id, user.id, by_id=uuid4())

    readded = await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"]])

    assert _names(readded.roles) == {"red_teamer"}


async def test_effective_object_permissions_member_is_held_role_union(db_session: AsyncSession) -> None:
    # A non-break-glass member's effective set is exactly their held roles' permissions.
    user = await _user(db_session)
    object_id = uuid4()
    role = Role(
        name=f"r-{uuid4().hex[:8]}",
        description="t",
        permissions=["evaluation_groups:read", "evaluation_groups:update"],
    )
    db_session.add(role)
    await db_session.flush()
    await grant_roles(db_session, _GROUP, object_id, user.id, [role])

    access = await resolve_object_access(db_session, _session_user(user, set()), _GROUP, object_id)

    assert effective_object_permissions(access) == frozenset({"evaluation_groups:read", "evaluation_groups:update"})


async def test_effective_object_permissions_break_glass_is_registry_vocabulary(db_session: AsyncSession) -> None:
    # A break-glass caller gets the object type's full vocabulary, derived from the
    # registry: every assignable role's permissions plus the break-glass key — even
    # as a non-member (no roles held).
    assert _SPEC.super_permission is not None
    user = await _user(db_session)
    caller = _session_user(user, {_SPEC.super_permission.value})

    access = await resolve_object_access(db_session, caller, _GROUP, uuid4())

    expected = {perm for role in _SPEC.assignable_system_roles for perm in ROLE_PERMISSIONS[role]}
    expected.add(_SPEC.super_permission.value)
    assert effective_object_permissions(access) == frozenset(expected)
    assert access.is_member is False


async def test_remove_non_member_raises(db_session: AsyncSession) -> None:
    user = await _user(db_session)

    with pytest.raises(NotFoundError):
        await remove_member(db_session, _GROUP, uuid4(), user.id, by_id=uuid4())


async def test_remove_member_rejects_last_owner(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    owner = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, owner, [system_roles["owner"]])

    with pytest.raises(ConflictError):
        await remove_member(db_session, _GROUP, object_id, owner.id, by_id=uuid4())

    # The sole owner is untouched — the guard fires before any soft-delete.
    assert _names(await held_roles(db_session, _GROUP, object_id, owner.id)) == {"owner"}


async def test_remove_member_allows_owner_when_co_owner_exists(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    first = await _user(db_session)
    second = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, first, [system_roles["owner"]])
    await add_member(db_session, _GROUP, object_id, second, [system_roles["owner"]])

    await remove_member(db_session, _GROUP, object_id, first.id, by_id=uuid4())

    assert await held_roles(db_session, _GROUP, object_id, first.id) == []
    assert _names(await held_roles(db_session, _GROUP, object_id, second.id)) == {"owner"}


async def test_remove_member_allows_non_owner(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    owner = await _user(db_session)
    other = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, owner, [system_roles["owner"]])
    await add_member(db_session, _GROUP, object_id, other, [system_roles["red_teamer"]])

    # Removing a non-owner never trips the guard, even though the owner is sole.
    await remove_member(db_session, _GROUP, object_id, other.id, by_id=uuid4())

    assert await held_roles(db_session, _GROUP, object_id, other.id) == []


async def test_set_member_roles_rejects_demoting_last_owner(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, owner, [system_roles["owner"]])

    with pytest.raises(ConflictError):
        await set_member_roles(db_session, _GROUP, object_id, owner, [system_roles["red_teamer"]], by_id=uuid4())

    assert _names(await held_roles(db_session, _GROUP, object_id, owner.id)) == {"owner"}


async def test_set_member_roles_allows_demoting_owner_when_co_owner_exists(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    first = await _user(db_session)
    second = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, first, [system_roles["owner"]])
    await add_member(db_session, _GROUP, object_id, second, [system_roles["owner"]])

    await set_member_roles(db_session, _GROUP, object_id, first, [system_roles["red_teamer"]], by_id=uuid4())

    assert _names(await held_roles(db_session, _GROUP, object_id, first.id)) == {"red_teamer"}


async def test_set_member_roles_allows_keeping_owner_while_adding_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, owner, [system_roles["owner"]])

    # Owner is retained, so the guard does not fire even as the sole owner.
    await set_member_roles(
        db_session, _GROUP, object_id, owner, [system_roles["owner"], system_roles["viewer"]], by_id=uuid4()
    )

    assert _names(await held_roles(db_session, _GROUP, object_id, owner.id)) == {"owner", "viewer"}


async def test_list_members_groups_by_user_and_paginates(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    object_id = uuid4()
    users = [await _user(db_session) for _ in range(3)]
    # One member holds two roles — must still be a single entry.
    await add_member(db_session, _GROUP, object_id, users[0], [system_roles["owner"], system_roles["viewer"]])
    await add_member(db_session, _GROUP, object_id, users[1], [system_roles["red_teamer"]])
    await add_member(db_session, _GROUP, object_id, users[2], [system_roles["viewer"]])

    first, total = await list_members(db_session, _GROUP, object_id, limit=2, offset=0)
    second, _ = await list_members(db_session, _GROUP, object_id, limit=2, offset=2)

    assert total == 3
    assert len(first) == 2
    assert len(second) == 1
    # Page boundary depends on UUID order, so assert over the union: every member
    # is one entry, and the two-role user's roles are grouped (not duplicated).
    members = {member.user.id: _names(member.roles) for member in [*first, *second]}
    assert members == {
        users[0].id: {"owner", "viewer"},
        users[1].id: {"red_teamer"},
        users[2].id: {"viewer"},
    }


async def test_list_members_role_id_filters_to_holders_keeping_full_role_set(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    object_id = uuid4()
    holder = await _user(db_session)
    other = await _user(db_session)
    await add_member(db_session, _GROUP, object_id, holder, [system_roles["annotator"], system_roles["viewer"]])
    await add_member(db_session, _GROUP, object_id, other, [system_roles["red_teamer"]])

    members, total = await list_members(
        db_session, _GROUP, object_id, role_id=system_roles["annotator"].id, limit=20, offset=0
    )

    # Only the annotator holder is returned, but with their *full* live role set.
    assert total == 1
    assert {member.user.id: _names(member.roles) for member in members} == {holder.id: {"annotator", "viewer"}}


async def test_list_members_excludes_soft_deleted_user(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    object_id = uuid4()
    live = await _user(db_session)
    gone = await _user(db_session)
    await add_member(db_session, _GROUP, object_id, live, [system_roles["annotator"]])
    await add_member(db_session, _GROUP, object_id, gone, [system_roles["annotator"]])
    gone.soft_delete(None)
    db_session.add(gone)
    await db_session.flush()

    members, total = await list_members(db_session, _GROUP, object_id, limit=20, offset=0)

    # The soft-deleted user's assignment is still live, but the user row is not;
    # excluded from both count and page, so total stays consistent with items.
    assert total == 1
    assert {member.user.id for member in members} == {live.id}


async def test_resolve_assignable_roles_rejects_platform_admin(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    with pytest.raises(BadRequestError):
        await resolve_assignable_roles(db_session, _SPEC, [system_roles[SystemRole.ADMIN.value].id])


async def test_resolve_assignable_roles_rejects_unknown_id(db_session: AsyncSession) -> None:
    with pytest.raises(BadRequestError):
        await resolve_assignable_roles(db_session, _SPEC, [uuid4()])


async def test_resolve_assignable_roles_rejects_inactive_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An inactive role is never assignable in-group, even one a member already holds.
    annotator = system_roles[SystemRole.ANNOTATOR.value]
    annotator.is_active = False
    db_session.add(annotator)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="inactive role"):
        await resolve_assignable_roles(db_session, _SPEC, [annotator.id])


async def test_object_member_response_hides_inactive_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    user = await _user(db_session)
    member = await add_member(db_session, _GROUP, uuid4(), user, [system_roles["owner"], system_roles["annotator"]])
    system_roles["annotator"].is_active = False
    db_session.add(system_roles["annotator"])
    await db_session.flush()

    response = ObjectMemberResponse.from_member(member)

    assert {role.id for role in response.roles} == {system_roles["owner"].id}


async def test_resolve_assignable_roles_accepts_opted_in_custom_role(db_session: AsyncSession) -> None:
    role = Role(
        name=f"lead-{uuid4().hex[:8]}",
        description="custom",
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
        is_object_assignable=True,
    )
    db_session.add(role)
    await db_session.flush()

    assert await resolve_assignable_roles(db_session, _SPEC, [role.id]) == [role]


async def test_resolve_assignable_roles_rejects_custom_role_without_opt_in(db_session: AsyncSession) -> None:
    role = Role(
        name=f"global-{uuid4().hex[:8]}",
        description="custom",
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    db_session.add(role)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="not assignable"):
        await resolve_assignable_roles(db_session, _SPEC, [role.id])


async def test_resolve_assignable_roles_rejects_opted_in_role_without_required_permission(
    db_session: AsyncSession,
) -> None:
    # Opting a role in isn't enough: without object-scope read its holder would sit
    # in the group reporting no read permission.
    role = Role(
        name=f"blind-{uuid4().hex[:8]}",
        description="custom",
        permissions=[Permission.FLAGS_CREATE.value],
        is_object_assignable=True,
    )
    db_session.add(role)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="evaluation_groups:read"):
        await resolve_assignable_roles(db_session, _SPEC, [role.id])


async def test_resolve_object_access_override_demotes_global_manager(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"]])
    caller = _session_user(user, {Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value})

    access = await resolve_object_access(db_session, caller, _GROUP, object_id)

    assert access.is_member is True
    # The held red_teamer role's own permissions are authoritative; the global
    # manage_members is ignored for this object.
    assert access.has(Permission.EVALUATION_GROUPS_MANAGE_MEMBERS) is False


async def test_resolve_object_access_break_glass_keeps_control(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    caller = _session_user(user, {Permission.EVALUATION_GROUPS_MANAGE.value})

    access = await resolve_object_access(db_session, caller, _GROUP, object_id)

    assert access.is_member is False
    assert access.has_super is True
    assert access.has(Permission.EVALUATION_GROUPS_MANAGE_MEMBERS) is True


async def test_resolve_object_access_ignores_inactive_roles(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    user = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, user, [system_roles["owner"]])
    system_roles["owner"].is_active = False
    await db_session.flush()

    access = await resolve_object_access(db_session, _session_user(user, set()), _GROUP, object_id)

    assert access.permissions == frozenset()
    assert access.is_member is True  # still a member for visibility


async def test_resolve_object_access_non_member_has_no_object_permissions(db_session: AsyncSession) -> None:
    # A global permission grants no object-scope authority — only an object role,
    # ownership, or break-glass does.
    user = await _user(db_session)
    caller = _session_user(user, {Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value})

    access = await resolve_object_access(db_session, caller, _GROUP, uuid4())

    assert access.is_member is False
    assert access.has(Permission.EVALUATION_GROUPS_MANAGE_MEMBERS) is False


async def test_objects_solely_held_by_reports_sole_owner(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, owner, [system_roles["owner"]])

    assert await objects_solely_held_by(db_session, owner.id) == [(_GROUP, object_id)]


async def test_objects_solely_held_by_empty_with_co_owner(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    first = await _user(db_session)
    second = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, first, [system_roles["owner"]])
    await add_member(db_session, _GROUP, object_id, second, [system_roles["owner"]])

    assert await objects_solely_held_by(db_session, first.id) == []


async def test_objects_solely_held_by_ignores_soft_deleted_co_owner(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A co-owner whose user row is soft-deleted is a ghost holder — it can't keep
    # the object owned, so the live owner is still reported as sole.
    live = await _user(db_session)
    ghost = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, live, [system_roles["owner"]])
    await add_member(db_session, _GROUP, object_id, ghost, [system_roles["owner"]])
    ghost.soft_delete(None)
    db_session.add(ghost)
    await db_session.flush()

    assert await objects_solely_held_by(db_session, live.id) == [(_GROUP, object_id)]


async def test_objects_solely_held_by_empty_for_non_owner(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    member = await _user(db_session)
    object_id = uuid4()
    await add_member(db_session, _GROUP, object_id, member, [system_roles["red_teamer"]])

    assert await objects_solely_held_by(db_session, member.id) == []


async def _named_user(db: AsyncSession, email: str) -> User:
    role = Role(name=f"r-{uuid4().hex[:8]}", description="test", permissions=[])
    db.add(role)
    await db.flush()
    return await create_user(db, email=email, roles=[role])


async def test_list_members_search_matches_email_case_insensitively(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    object_id = uuid4()
    alice = await _named_user(db_session, "alice-search@example.com")
    bob = await _named_user(db_session, "bob-search@example.com")
    await add_member(db_session, _GROUP, object_id, alice, [system_roles["red_teamer"]])
    await add_member(db_session, _GROUP, object_id, bob, [system_roles["red_teamer"]])

    found, total = await list_members(db_session, _GROUP, object_id, search="ALICE", limit=50, offset=0)

    assert total == 1
    assert {member.user.id for member in found} == {alice.id}


async def test_list_members_search_treats_like_metacharacters_literally(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A lone '%' must NOT act as a wildcard (would otherwise match every member).
    object_id = uuid4()
    user = await _named_user(db_session, "literal@example.com")
    await add_member(db_session, _GROUP, object_id, user, [system_roles["red_teamer"]])

    found, total = await list_members(db_session, _GROUP, object_id, search="%", limit=50, offset=0)

    assert total == 0
    assert found == []


async def test_holds_permission_anywhere_requires_a_live_active_assignment(
    db_session: AsyncSession,
) -> None:
    """Freezes the liveness rule of the loader-option form.

    `holds_permission_anywhere` expresses liveness as `with_live(ObjectRoleAssignment)` over a
    join, where `groups_granting` in `evaluations/access.py` writes `deleted_at IS NULL` by hand.
    Both are correct; only this one hides the predicate in a loader option, so a unit test is what
    keeps a later "cleanup" from dropping it silently.
    """
    # A throwaway role, not a system one: `red_teamer` is the default role here, and
    # `ck_roles_default_role_active` forbids deactivating whichever role that is.
    role = Role(
        name=f"flagger-{uuid4().hex[:8]}", description="grants flag create", permissions=[Permission.FLAGS_CREATE.value]
    )
    db_session.add(role)
    await db_session.flush()
    user = await _user(db_session)
    object_id = uuid4()

    assert not await holds_permission_anywhere(db_session, user.id, _GROUP, Permission.FLAGS_CREATE)

    await grant_roles(db_session, _GROUP, object_id, user.id, [role])
    assert await holds_permission_anywhere(db_session, user.id, _GROUP, Permission.FLAGS_CREATE)

    # A removed member grants nothing — the assignment is tombstoned, not deleted.
    await remove_member(db_session, _GROUP, object_id, user.id, by_id=uuid4())
    assert not await holds_permission_anywhere(db_session, user.id, _GROUP, Permission.FLAGS_CREATE)

    # Re-granted, then the role itself deactivated: still nothing.
    await grant_roles(db_session, _GROUP, object_id, user.id, [role])
    role.is_active = False
    db_session.add(role)
    await db_session.flush()
    assert not await holds_permission_anywhere(db_session, user.id, _GROUP, Permission.FLAGS_CREATE)


async def test_assert_object_permission_refuses_break_glass_and_missing_object(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The object arm reads the membership set only — never `ObjectAccessContext.has`.

    `evaluation_groups:manage` is delegable, so honouring it here would let a custom role carrying
    just the break-glass key stand in for a capability the caller was never granted. `None` is a
    refusal for the same reason: a request that named no object must not widen anything.
    """
    user = await _user(db_session)
    object_id = uuid4()
    manager = _session_user(user, {Permission.EVALUATION_GROUPS_MANAGE.value})

    with pytest.raises(ForbiddenError):
        await assert_object_permission(db_session, manager, _GROUP, object_id, Permission.CONVERSATIONS_READ)
    with pytest.raises(ForbiddenError):
        await assert_object_permission(db_session, manager, _GROUP, None, Permission.CONVERSATIONS_READ)

    # Held in-group, it passes — the same caller, the same object.
    await grant_roles(db_session, _GROUP, object_id, user.id, [system_roles[SystemRole.RED_TEAMER.value]])
    await assert_object_permission(db_session, manager, _GROUP, object_id, Permission.CONVERSATIONS_READ)
