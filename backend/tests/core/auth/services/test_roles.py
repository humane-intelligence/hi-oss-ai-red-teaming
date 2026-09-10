"""Tests for `app.core.auth.services.roles` — sync, catalog, and custom-role management."""

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import OBJECT_ASSIGNABLE_SYSTEM_ROLES
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import remove_member
from app.core.auth.roles import DEFAULT_PARTICIPANT_ROLE
from app.core.auth.roles import NON_DELEGABLE_PERMISSIONS
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services import session_revocation
from app.core.auth.services.roles import create_role
from app.core.auth.services.roles import delete_role
from app.core.auth.services.roles import effective_permissions
from app.core.auth.services.roles import get_default_role
from app.core.auth.services.roles import get_participant_default_role
from app.core.auth.services.roles import get_role_by_name
from app.core.auth.services.roles import set_default_role
from app.core.auth.services.roles import set_participant_default_role
from app.core.auth.services.roles import set_role_active
from app.core.auth.services.roles import sync_system_roles
from app.core.auth.services.roles import update_role
from app.core.auth.services.session_revocation import is_revoked
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from tests.conftest import persist_evaluation_group


async def _clear_revocation(user_id: UUID) -> None:
    async with session_revocation._redis() as client:
        await client.delete(session_revocation._key(user_id))


async def _grantable_role(db: AsyncSession, *, name: str) -> Role:
    """A custom role self-join could grant: object-assignable and carrying in-group read."""
    role = await create_role(
        db,
        name=name,
        display_name=name,
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    return await update_role(db, role, is_object_assignable=True)


@pytest.mark.integration
async def test_sync_creates_all_canonical_roles(db_session: AsyncSession) -> None:
    roles = await sync_system_roles(db_session)

    assert set(roles.keys()) == {role.value for role in SystemRole}
    red_teamer = roles[SystemRole.RED_TEAMER]
    assert red_teamer.id is not None
    assert red_teamer.display_name == "Red Teamer"
    assert red_teamer.permissions == sorted(ROLE_PERMISSIONS[SystemRole.RED_TEAMER])
    assert red_teamer.is_system is True


@pytest.mark.integration
async def test_sync_overwrites_existing_row(db_session: AsyncSession) -> None:
    # `is_system=False` mirrors a row that pre-dated the column — sync must
    # promote it to true so legacy seeds don't stay editable forever.
    stale = Role(
        name="admin",
        display_name="stale",
        description="stale",
        permissions=["stale:perm"],
        is_system=False,
    )
    db_session.add(stale)
    await db_session.flush()

    roles = await sync_system_roles(db_session)

    admin = roles[SystemRole.ADMIN]
    assert admin.id == stale.id
    assert admin.display_name == SystemRole.ADMIN.spec.display_name
    assert admin.permissions == sorted(ROLE_PERMISSIONS[SystemRole.ADMIN])
    assert admin.is_system is True


@pytest.mark.integration
async def test_sync_is_idempotent(db_session: AsyncSession) -> None:
    first = await sync_system_roles(db_session)
    first_ids = {name: role.id for name, role in first.items()}

    second = await sync_system_roles(db_session)
    second_ids = {name: role.id for name, role in second.items()}

    assert first_ids == second_ids
    live = (await db_session.execute(select(Role).where(col(Role.deleted_at).is_(None)))).scalars().all()
    assert len(live) == len(SystemRole)


@pytest.mark.integration
async def test_sync_leaves_non_canonical_roles_untouched(db_session: AsyncSession) -> None:
    legacy = Role(name="participant", description="legacy", permissions=[])
    db_session.add(legacy)
    await db_session.flush()

    await sync_system_roles(db_session)

    survivor = (
        await db_session.execute(Role.live_select().where(col(Role.name) == "participant"))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.id == legacy.id
    # Non-canonical rows keep the column default — sync only flips canonical roles.
    assert survivor.is_system is False


@pytest.mark.integration
async def test_sync_creates_active_roles_and_one_default(db_session: AsyncSession) -> None:
    roles = await sync_system_roles(db_session)

    assert all(role.is_active is True for role in roles.values())
    assert roles[DEFAULT_PARTICIPANT_ROLE].is_default is True
    assert [name for name, role in roles.items() if role.is_default] == [DEFAULT_PARTICIPANT_ROLE]


@pytest.mark.integration
async def test_sync_seeds_one_participant_default(db_session: AsyncSession) -> None:
    roles = await sync_system_roles(db_session)

    assert roles[DEFAULT_PARTICIPANT_ROLE].is_participant_default is True
    assert [name for name, role in roles.items() if role.is_participant_default] == [DEFAULT_PARTICIPANT_ROLE]


@pytest.mark.integration
async def test_sync_seeds_object_assignability_from_the_registry(db_session: AsyncSession) -> None:
    roles = await sync_system_roles(db_session)

    assignable = {name for name, role in roles.items() if role.is_object_assignable}
    assert assignable == {role.value for role in OBJECT_ASSIGNABLE_SYSTEM_ROLES}
    assert roles[SystemRole.ADMIN].is_object_assignable is False


@pytest.mark.integration
async def test_sync_reconverges_object_assignability(db_session: AsyncSession) -> None:
    # Unlike the default flags, object-assignability is platform policy — a row
    # edited away from the registry must be corrected on the next deploy.
    await sync_system_roles(db_session)
    admin = await get_role_by_name(db_session, SystemRole.ADMIN.value)
    admin.is_object_assignable = True
    viewer = await get_role_by_name(db_session, SystemRole.VIEWER.value)
    viewer.is_object_assignable = False
    await db_session.flush()

    await sync_system_roles(db_session)

    assert (await get_role_by_name(db_session, SystemRole.ADMIN.value)).is_object_assignable is False
    assert (await get_role_by_name(db_session, SystemRole.VIEWER.value)).is_object_assignable is True


@pytest.mark.integration
async def test_sync_preserves_activation_flags_on_existing_rows(db_session: AsyncSession) -> None:
    # Create-only: an operator-deactivated system role must survive a re-sync.
    await sync_system_roles(db_session)
    viewer = await get_role_by_name(db_session, SystemRole.VIEWER.value)
    viewer.is_active = False
    await db_session.flush()

    await sync_system_roles(db_session)

    assert (await get_role_by_name(db_session, SystemRole.VIEWER.value)).is_active is False


@pytest.mark.integration
async def test_get_default_role_returns_active_default(db_session: AsyncSession) -> None:
    await sync_system_roles(db_session)

    default = await get_default_role(db_session)

    assert default.name == DEFAULT_PARTICIPANT_ROLE.value
    assert default.is_default is True


@pytest.mark.integration
async def test_get_default_role_raises_when_absent(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError):
        await get_default_role(db_session)


@pytest.mark.integration
async def test_get_participant_default_role_returns_active_default(db_session: AsyncSession) -> None:
    await sync_system_roles(db_session)

    default = await get_participant_default_role(db_session)

    assert default.name == DEFAULT_PARTICIPANT_ROLE.value
    assert default.is_participant_default is True


@pytest.mark.integration
async def test_get_participant_default_role_raises_when_absent(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError):
        await get_participant_default_role(db_session)


@pytest.mark.integration
async def test_default_role_cannot_be_deactivated(db_session: AsyncSession) -> None:
    # The `default_role_active` check constraint blocks the inactive-default path
    # at the DB — otherwise get_default_role would raise and 500 every registration.
    role = Role(name="a-default", is_default=True, is_active=True)
    db_session.add(role)
    await db_session.flush()

    role.is_active = False
    db_session.add(role)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_participant_default_role_cannot_be_deactivated(db_session: AsyncSession) -> None:
    # The `participant_default_role_active` check constraint mirrors the new-user
    # default: an inactive participant default would 500 every self-join.
    role = Role(name="a-participant-default", is_participant_default=True, is_active=True, is_object_assignable=True)
    db_session.add(role)
    await db_session.flush()

    role.is_active = False
    db_session.add(role)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_participant_default_role_must_be_object_assignable(db_session: AsyncSession) -> None:
    # Self-join grants the participant default as an object role directly, without
    # passing through assignment validation — so the DB refuses a participant default
    # that validation would have rejected.
    role = Role(name="a-global-only-default", is_participant_default=True, is_active=True, is_object_assignable=True)
    db_session.add(role)
    await db_session.flush()

    role.is_object_assignable = False
    db_session.add(role)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.unit
def test_effective_permissions_drops_inactive_roles() -> None:
    user = User(email="ep@example.com")
    user.roles = [
        Role(name="active", permissions=["a:1"], is_active=True),
        Role(name="inactive", permissions=["b:2"], is_active=False),
    ]

    assert effective_permissions(user) == ["a:1"]


@pytest.mark.integration
async def test_create_role_persists_active_non_system_role(db_session: AsyncSession) -> None:
    role = await create_role(
        db_session,
        name="reviewer-plus",
        display_name="Reviewer Plus",
        description="Custom reviewer",
        # Duplicate + unsorted: the service stores them sorted and deduped.
        permissions=[
            Permission.SAVED_VIEWS_READ.value,
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_READ.value,
        ],
    )

    assert role.id is not None
    assert (role.is_system, role.is_active, role.is_default) == (False, True, False)
    assert role.permissions == sorted({Permission.SAVED_VIEWS_READ.value, Permission.EVALUATIONS_READ.value})


@pytest.mark.integration
async def test_create_role_rejects_unknown_permission(db_session: AsyncSession) -> None:
    with pytest.raises(BadRequestError, match="Unknown permission"):
        await create_role(db_session, name="bad", display_name="Bad", description=None, permissions=["not:a:perm"])


@pytest.mark.integration
@pytest.mark.parametrize("permission", sorted(NON_DELEGABLE_PERMISSIONS))
async def test_create_role_rejects_non_delegable_permission(db_session: AsyncSession, permission: Permission) -> None:
    # `users:update` is among these because it replaces a user's whole role set, so its
    # holder could grant themselves any role `ELEVATED_ROLE_PERMISSIONS` doesn't gate.
    with pytest.raises(BadRequestError, match="cannot be granted"):
        await create_role(
            db_session,
            name=f"sneaky-{permission.value.replace(':', '-')}",
            display_name="Sneaky",
            description=None,
            permissions=[permission.value],
        )


@pytest.mark.integration
async def test_create_role_rejects_reserved_name(db_session: AsyncSession) -> None:
    with pytest.raises(ConflictError, match="reserved"):
        await create_role(
            db_session, name=SystemRole.ADMIN.value, display_name="Admin", description=None, permissions=[]
        )


@pytest.mark.integration
async def test_create_role_rejects_duplicate_name(db_session: AsyncSession) -> None:
    await create_role(db_session, name="dup", display_name="Dup", description=None, permissions=[])

    with pytest.raises(ConflictError, match="already exists"):
        await create_role(db_session, name="dup", display_name="Dup 2", description=None, permissions=[])


@pytest.mark.integration
async def test_update_role_edits_custom_fields(db_session: AsyncSession) -> None:
    role = await create_role(
        db_session,
        name="editme",
        display_name="Editme",
        description="old",
        permissions=[Permission.EVALUATIONS_READ.value],
    )

    await update_role(
        db_session,
        role,
        display_name="Renamed",
        description="new",
        description_provided=True,
        permissions=[Permission.MODELS_READ.value],
    )

    assert (role.display_name, role.description) == ("Renamed", "new")
    assert role.permissions == [Permission.MODELS_READ.value]


@pytest.mark.integration
async def test_update_role_rejects_system_role(db_session: AsyncSession) -> None:
    await sync_system_roles(db_session)
    admin = await get_role_by_name(db_session, SystemRole.ADMIN.value)

    with pytest.raises(BadRequestError, match="code-managed"):
        await update_role(db_session, admin, display_name="Hacked")


@pytest.mark.integration
async def test_update_role_rejects_non_delegable_permission(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="upd", display_name="Upd", description=None, permissions=[])

    with pytest.raises(BadRequestError, match="cannot be granted"):
        await update_role(db_session, role, permissions=[Permission.USERS_MANAGE_ADMIN.value])


@pytest.mark.integration
async def test_delete_role_soft_deletes_custom_role(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="goner", display_name="Goner", description=None, permissions=[])

    await delete_role(db_session, role, by_id=uuid4())

    assert role.deleted_at is not None
    assert await db_session.scalar(Role.live_select().where(col(Role.id) == role.id)) is None


@pytest.mark.integration
async def test_delete_role_rejects_system_role(db_session: AsyncSession) -> None:
    await sync_system_roles(db_session)
    viewer = await get_role_by_name(db_session, SystemRole.VIEWER.value)

    with pytest.raises(BadRequestError, match="cannot be deleted"):
        await delete_role(db_session, viewer, by_id=uuid4())


@pytest.mark.integration
async def test_set_role_active_toggles_custom_role(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="toggle", display_name="Toggle", description=None, permissions=[])

    await set_role_active(db_session, role, is_active=False)
    assert role.is_active is False

    await set_role_active(db_session, role, is_active=True)
    assert role.is_active is True


@pytest.mark.integration
async def test_set_role_active_noop_returns_early(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="noop", display_name="Noop", description=None, permissions=[])

    result = await set_role_active(db_session, role, is_active=True)

    assert result.is_active is True


@pytest.mark.integration
@pytest.mark.parametrize("role_name", [SystemRole.ADMIN.value, SystemRole.OWNER.value])
async def test_set_role_active_rejects_deactivating_protected_role(
    db_session: AsyncSession, system_roles: dict[str, Role], role_name: str
) -> None:
    with pytest.raises(ConflictError, match="cannot be deactivated"):
        await set_role_active(db_session, system_roles[role_name], is_active=False)


@pytest.mark.integration
async def test_set_role_active_rejects_deactivating_default(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    with pytest.raises(ConflictError, match="default"):
        await set_role_active(db_session, system_roles[DEFAULT_PARTICIPANT_ROLE.value], is_active=False)


@pytest.mark.integration
async def test_set_role_active_rejects_deactivating_participant_default(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # red_teamer is seeded as both defaults; move the new-user default away so only
    # the participant-default guard remains in play.
    await set_default_role(db_session, system_roles[SystemRole.VIEWER.value])

    with pytest.raises(ConflictError, match="participant default"):
        await set_role_active(db_session, system_roles[DEFAULT_PARTICIPANT_ROLE.value], is_active=False)


@pytest.mark.integration
async def test_set_role_active_rejects_stranding_sole_global_holder(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="solo", display_name="Solo", description=None, permissions=[])
    await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])

    with pytest.raises(ConflictError, match="only active role for 1 user"):
        await set_role_active(db_session, role, is_active=False)


@pytest.mark.integration
async def test_set_role_active_rejects_stranding_sole_object_member(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A group member whose only in-group role is `annotator` (a deactivatable
    # system role) would be left role-less on the group — the object arm of the guard.
    annotator = system_roles[SystemRole.ANNOTATOR.value]
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    member = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[system_roles["red_teamer"]])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [annotator])

    with pytest.raises(ConflictError, match="group member"):
        await set_role_active(db_session, annotator, is_active=False)


@pytest.mark.integration
async def test_set_role_active_deactivation_revokes_holder_when_not_stranded(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    role = await create_role(db_session, name="extra", display_name="Extra", description=None, permissions=[])
    holder = await create_user(
        db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role, system_roles[SystemRole.VIEWER.value]]
    )
    holder_id = holder.id

    try:
        await set_role_active(db_session, role, is_active=False)

        assert role.is_active is False
        assert await is_revoked(holder_id, 1) is True
    finally:
        await _clear_revocation(holder_id)


@pytest.mark.integration
async def test_set_default_role_reassigns_atomically(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    viewer = system_roles[SystemRole.VIEWER.value]

    await set_default_role(db_session, viewer)

    assert viewer.is_default is True
    assert system_roles[DEFAULT_PARTICIPANT_ROLE.value].is_default is False
    assert (await get_default_role(db_session)).id == viewer.id


@pytest.mark.integration
async def test_set_default_role_rejects_inactive_target(db_session: AsyncSession) -> None:
    role = Role(name="inactive-candidate", is_active=False)
    db_session.add(role)
    await db_session.flush()

    with pytest.raises(ConflictError, match="inactive"):
        await set_default_role(db_session, role)


@pytest.mark.integration
@pytest.mark.parametrize("role_name", [SystemRole.ADMIN.value, SystemRole.OWNER.value])
async def test_set_default_role_rejects_privileged_role(
    db_session: AsyncSession, system_roles: dict[str, Role], role_name: str
) -> None:
    with pytest.raises(ConflictError, match="cannot be made a default"):
        await set_default_role(db_session, system_roles[role_name])


@pytest.mark.integration
async def test_set_participant_default_role_reassigns_atomically(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    viewer = system_roles[SystemRole.VIEWER.value]

    await set_participant_default_role(db_session, viewer)

    assert viewer.is_participant_default is True
    assert system_roles[DEFAULT_PARTICIPANT_ROLE.value].is_participant_default is False
    assert (await get_participant_default_role(db_session)).id == viewer.id


@pytest.mark.integration
async def test_set_participant_default_role_rejects_inactive_target(db_session: AsyncSession) -> None:
    role = Role(name="inactive-participant", is_active=False)
    db_session.add(role)
    await db_session.flush()

    with pytest.raises(ConflictError, match="inactive"):
        await set_participant_default_role(db_session, role)


@pytest.mark.integration
@pytest.mark.parametrize("role_name", [SystemRole.ADMIN.value, SystemRole.OWNER.value])
async def test_set_participant_default_role_rejects_privileged_role(
    db_session: AsyncSession, system_roles: dict[str, Role], role_name: str
) -> None:
    with pytest.raises(ConflictError, match="cannot be made a default"):
        await set_participant_default_role(db_session, system_roles[role_name])


@pytest.mark.integration
async def test_set_participant_default_role_rejects_role_not_object_assignable(db_session: AsyncSession) -> None:
    # Self-join mints an object role, so a global-only role can't be the default.
    role = await create_role(
        db_session,
        name="global-only",
        display_name="Global Only",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )

    with pytest.raises(ConflictError, match="not assignable in an evaluation group"):
        await set_participant_default_role(db_session, role)


@pytest.mark.integration
async def test_set_participant_default_role_rejects_role_without_group_read(db_session: AsyncSession) -> None:
    # `update_role` refuses this combination, so reach it the only other way a row
    # could (a direct write) — the setter must not rely on that guard alone.
    role = await create_role(
        db_session, name="blind-participant", display_name="Blind", description=None, permissions=[]
    )
    role.is_object_assignable = True
    db_session.add(role)
    await db_session.flush()

    with pytest.raises(ConflictError, match="evaluation_groups:read"):
        await set_participant_default_role(db_session, role)


@pytest.mark.integration
async def test_update_role_toggles_object_assignability(db_session: AsyncSession) -> None:
    role = await create_role(
        db_session,
        name="in-group",
        display_name="In Group",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    assert role.is_object_assignable is False

    await update_role(db_session, role, is_object_assignable=True)
    assert role.is_object_assignable is True

    await update_role(db_session, role, is_object_assignable=False)
    assert role.is_object_assignable is False


@pytest.mark.integration
async def test_update_role_rejects_making_a_read_less_role_object_assignable(db_session: AsyncSession) -> None:
    # The flag would claim the role is in-group assignable while assignment rejects it.
    role = await create_role(db_session, name="no-read", display_name="No Read", description=None, permissions=[])

    with pytest.raises(ConflictError, match="evaluation_groups:read"):
        await update_role(db_session, role, is_object_assignable=True)


@pytest.mark.integration
async def test_update_role_rejects_dropping_group_read_from_an_assignable_role(db_session: AsyncSession) -> None:
    # The hole a flag-only guard leaves: same broken end state via a permissions edit.
    role = await _grantable_role(db_session, name="loses-read")

    with pytest.raises(ConflictError, match="evaluation_groups:read"):
        await update_role(db_session, role, permissions=[Permission.SAVED_VIEWS_READ.value])


@pytest.mark.integration
async def test_update_role_cannot_unassign_role_held_by_a_member(db_session: AsyncSession) -> None:
    # Un-flagging keeps the live assignment working, but the next edit of that member's
    # roles couldn't preserve it — they'd lose the group.
    role = await _grantable_role(db_session, name="sole-in-group")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    member = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [role])

    with pytest.raises(ConflictError, match="still held by"):
        await update_role(db_session, role, is_object_assignable=False)


@pytest.mark.integration
async def test_update_role_cannot_unassign_role_held_alongside_another_object_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Stricter than the sole-role guard: the member keeps the group, but would still
    # lose this role on the next edit of their role set, silently.
    role = await _grantable_role(db_session, name="second-in-group")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    member = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [role, system_roles[SystemRole.VIEWER.value]]
    )

    with pytest.raises(ConflictError, match="still held by"):
        await update_role(db_session, role, is_object_assignable=False)


@pytest.mark.integration
async def test_update_role_can_unassign_after_the_member_is_removed(db_session: AsyncSession) -> None:
    role = await _grantable_role(db_session, name="freed-in-group")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    member = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [role])
    await remove_member(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, by_id=uuid4())

    await update_role(db_session, role, is_object_assignable=False)

    assert role.is_object_assignable is False


@pytest.mark.integration
async def test_update_role_cannot_unassign_the_participant_default(db_session: AsyncSession) -> None:
    # Clearing the flag would leave self-join granting a role assignment rejects.
    role = await _grantable_role(db_session, name="held-participant")
    await set_participant_default_role(db_session, role)

    with pytest.raises(ConflictError, match="group self-join default"):
        await update_role(db_session, role, is_object_assignable=False)


@pytest.mark.integration
async def test_delete_role_rejects_default(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="cust-default", display_name="CD", description=None, permissions=[])
    await set_default_role(db_session, role)

    with pytest.raises(ConflictError, match="default"):
        await delete_role(db_session, role, by_id=uuid4())


@pytest.mark.integration
async def test_delete_role_rejects_participant_default(db_session: AsyncSession) -> None:
    role = await _grantable_role(db_session, name="cust-participant")
    await set_participant_default_role(db_session, role)

    with pytest.raises(ConflictError, match="participant default"):
        await delete_role(db_session, role, by_id=uuid4())


@pytest.mark.integration
async def test_delete_role_rejects_stranding_sole_holder(db_session: AsyncSession) -> None:
    role = await create_role(db_session, name="del-sole", display_name="Del Sole", description=None, permissions=[])
    await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])

    with pytest.raises(ConflictError, match="reassign them first"):
        await delete_role(db_session, role, by_id=uuid4())


@pytest.mark.integration
async def test_update_role_permission_removal_revokes_holders(db_session: AsyncSession) -> None:
    role = await create_role(
        db_session,
        name="shrink",
        display_name="Shrink",
        description=None,
        permissions=[Permission.EVALUATIONS_READ.value, Permission.MODELS_READ.value],
    )
    holder = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    holder_id = holder.id

    try:
        await update_role(db_session, role, permissions=[Permission.EVALUATIONS_READ.value])

        assert await is_revoked(holder_id, 1) is True
    finally:
        await _clear_revocation(holder_id)


@pytest.mark.integration
async def test_update_role_permission_addition_does_not_revoke(db_session: AsyncSession) -> None:
    role = await create_role(
        db_session, name="grow", display_name="Grow", description=None, permissions=[Permission.EVALUATIONS_READ.value]
    )
    holder = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    holder_id = holder.id

    try:
        await update_role(
            db_session, role, permissions=[Permission.EVALUATIONS_READ.value, Permission.MODELS_READ.value]
        )

        assert await is_revoked(holder_id, 1) is False
    finally:
        await _clear_revocation(holder_id)
