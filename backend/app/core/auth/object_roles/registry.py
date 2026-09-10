"""Per-object-type policy: which roles are assignable, and the break-glass key.

The object-role layer reuses the platform RBAC vocabulary directly — a held
object role's permissions are its `Role.permissions` (the field
`sync_system_roles` writes from `app/core/auth/roles.py`), not a second copy. So
this registry holds only what *is* object-type policy and can't come from a role
row: the set of roles a caller may assign on the object, and the global
permission that acts as the break-glass override.

The trade-off of one vocabulary: editing a platform role's spec retroactively
changes what every existing in-object holder of that role can do, and a role
"holds" its platform-only permissions at object scope too (in-group `owner`
nominally holds `users:invite`, which means nothing on a group). Accepted for
the simplicity of a single permission set; the alternative is a parallel
object-scope vocabulary to maintain.

The trade-off of one flag: `OBJECT_ASSIGNABLE_SYSTEM_ROLES` unions the per-type
sets so `sync_system_roles` can project them onto `Role.is_object_assignable`,
the single column assignment validates (custom roles opt in on that column too).
A row-level flag can't express "assignable on type A but not type B", so the
projection is lossless only while every spec declares the same system roles — a
unit test in `tests/unit/test_rbac_catalog.py` pins the equivalence and fails the
moment a second type diverges, at which point assignment needs the per-type check
back alongside the flag.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole


class ObjectType(StrEnum):
    """Kinds of object that can carry per-object role assignments.

    The value is the stable slug stored in `object_role_assignments.object_type`
    and used as the `OBJECT_ROLE_REGISTRY` key.
    """

    EVALUATION_GROUP = "evaluation_group"


@dataclass(frozen=True, slots=True)
class ObjectScopeSpec:
    """Per-object-type authorization policy.

    Attributes:
        assignable_system_roles: *System* roles a caller may assign on this object
            type — a subset, not the whole assignable set: custom roles opt in per
            row via `Role.is_object_assignable`, which is what assignment actually
            validates. This set is the code-owned policy `sync_system_roles`
            projects onto that column for the canonical roles. `admin` is
            intentionally excluded so it can never be granted as an in-object role.
            (What each role *grants* in-object is its `Role.permissions`, not
            declared here.)
        required_permission: Permission every role assignable here must grant,
            enforced by `resolve_assignable_roles`. `None` imposes no requirement.
        super_permission: Global break-glass permission that bypasses the
            per-object override — a caller carrying it in the JWT keeps full
            object control regardless of any (lesser) object role. `None`
            disables the escape hatch for this type.
        protected_role: A role that must always retain at least one live holder
            on the object — its last holder can be neither removed nor demoted,
            so the object can't be left without that authority. `None` imposes no
            such invariant. The guard is absolute (the member-management gate is
            object-scope, so even a break-glass admin must hand the role off
            rather than orphan the object).
    """

    assignable_system_roles: frozenset[SystemRole]
    super_permission: Permission | None = None
    protected_role: SystemRole | None = None
    required_permission: Permission | None = None


OBJECT_ROLE_REGISTRY: dict[ObjectType, ObjectScopeSpec] = {
    # `required_permission` enforces what used to be a prose invariant: the
    # visibility filter in `app/core/evaluations/access.py` treats *any* live
    # assignment as read access, so a role assignable here that didn't grant
    # `evaluation_groups:read` would leave a member inside a group whose reported
    # object permissions omit read — incoherent now that operators can opt custom
    # roles in.
    ObjectType.EVALUATION_GROUP: ObjectScopeSpec(
        assignable_system_roles=frozenset(
            {SystemRole.OWNER, SystemRole.RED_TEAMER, SystemRole.ANNOTATOR, SystemRole.VIEWER}
        ),
        super_permission=Permission.EVALUATION_GROUPS_MANAGE,
        protected_role=SystemRole.OWNER,
        required_permission=Permission.EVALUATION_GROUPS_READ,
    ),
}


# Union across specs, projected onto `Role.is_object_assignable` (see module docstring).
OBJECT_ASSIGNABLE_SYSTEM_ROLES: frozenset[SystemRole] = frozenset(
    role for spec in OBJECT_ROLE_REGISTRY.values() for role in spec.assignable_system_roles
)
