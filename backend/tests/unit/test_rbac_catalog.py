"""RBAC vocabulary invariants — the permission catalog stays complete."""

import pytest

from app.core.auth.object_roles.registry import OBJECT_ASSIGNABLE_SYSTEM_ROLES
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.roles import DEFAULT_PARTICIPANT_ROLE
from app.core.auth.roles import ELEVATED_ROLE_PERMISSIONS
from app.core.auth.roles import NON_DEACTIVATABLE_SYSTEM_ROLES
from app.core.auth.roles import NON_DELEGABLE_PERMISSIONS
from app.core.auth.roles import PERMISSION_DESCRIPTIONS
from app.core.auth.roles import RESERVED_ROLE_NAMES
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole


@pytest.mark.unit
def test_every_permission_has_a_description() -> None:
    missing = [p.value for p in Permission if p not in PERMISSION_DESCRIPTIONS]

    assert not missing, f"permissions without a description: {missing}"


@pytest.mark.unit
def test_permission_descriptions_are_non_empty() -> None:
    blank = [p.value for p, desc in PERMISSION_DESCRIPTIONS.items() if not desc.strip()]

    assert not blank, f"permissions with a blank description: {blank}"


@pytest.mark.unit
def test_descriptions_do_not_cover_unknown_permissions() -> None:
    assert set(PERMISSION_DESCRIPTIONS) == set(Permission)


@pytest.mark.unit
@pytest.mark.parametrize("role", [SystemRole.ADMIN, SystemRole.OWNER])
def test_role_assigners_can_read_the_role_catalog(role: SystemRole) -> None:
    assert Permission.ROLES_READ in role.spec.permissions


@pytest.mark.unit
def test_roles_manage_is_admin_only() -> None:
    holders = [role for role in SystemRole if Permission.ROLES_MANAGE in role.spec.permissions]

    assert holders == [SystemRole.ADMIN]


@pytest.mark.unit
def test_reserved_role_names_cover_every_system_role() -> None:
    assert {role.value for role in SystemRole} == RESERVED_ROLE_NAMES


@pytest.mark.unit
def test_non_deactivatable_roles_are_admin_and_owner_only() -> None:
    # annotator (decoupled via reviews:annotate) and viewer carry no code reference
    # a deactivation would break, so both stay operator-deactivatable.
    assert {SystemRole.ADMIN, SystemRole.OWNER} == NON_DEACTIVATABLE_SYSTEM_ROLES


@pytest.mark.unit
@pytest.mark.parametrize("permission", sorted(NON_DELEGABLE_PERMISSIONS))
def test_non_delegable_permissions_are_held_only_by_admin(permission: Permission) -> None:
    # Both are elevation vectors: no non-admin system role carries them, and a custom
    # role can't (create/update reject the set), so admin is the sole holder.
    holders = {role for role in SystemRole if permission in role.spec.permissions}

    assert holders == {SystemRole.ADMIN}


@pytest.mark.unit
def test_assignable_system_roles_grant_their_types_required_permission() -> None:
    # `resolve_assignable_roles` enforces this at runtime, so a role added to a
    # spec without the permission would seed as assignable and then 400 on every
    # assignment. Catch it where the mistake is made — editing the registry.
    lacking = [
        (object_type.value, role.value)
        for object_type, spec in OBJECT_ROLE_REGISTRY.items()
        if spec.required_permission is not None
        for role in spec.assignable_system_roles
        if spec.required_permission not in role.spec.permissions
    ]

    assert not lacking, f"assignable system roles missing their type's required permission: {lacking}"


@pytest.mark.unit
def test_object_assignable_flag_is_lossless() -> None:
    # `Role.is_object_assignable` is the union across specs, and assignment reads only
    # the flag — equivalent to per-type policy just while the specs agree. A second
    # object type with a different set would silently inherit the first's roles.
    divergent = {
        object_type.value: sorted(role.value for role in spec.assignable_system_roles ^ OBJECT_ASSIGNABLE_SYSTEM_ROLES)
        for object_type, spec in OBJECT_ROLE_REGISTRY.items()
        if spec.assignable_system_roles != OBJECT_ASSIGNABLE_SYSTEM_ROLES
    }

    assert not divergent, (
        "per-type assignable system roles diverge from the flag's union "
        f"({divergent}); assignment must check the spec again, not just the flag"
    )


@pytest.mark.unit
def test_elevated_role_gates_are_non_delegable() -> None:
    # An operator-defined role holding a gate permission could mint the role it gates,
    # so the gate holds only while every key is barred from custom permission sets.
    assert set(ELEVATED_ROLE_PERMISSIONS.values()) <= NON_DELEGABLE_PERMISSIONS


@pytest.mark.unit
def test_default_participant_role_is_object_assignable() -> None:
    # It is seeded as the self-join participant default, which a check constraint
    # requires to be object-assignable — dropping it from the registry would surface
    # as an IntegrityError from `sync_system_roles` on deploy, not here.
    assert DEFAULT_PARTICIPANT_ROLE in OBJECT_ASSIGNABLE_SYSTEM_ROLES
