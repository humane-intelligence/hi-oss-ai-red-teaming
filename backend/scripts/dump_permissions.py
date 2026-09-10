"""Dump the canonical RBAC vocabulary to a roles/permissions matrix in ``docs/permissions.md``.

Pure introspection of the two RBAC sources of truth — ``app.core.auth.roles``
(roles, permissions, the global role→permission map) and
``app.core.auth.object_roles.registry`` (per-object-type assignable roles and the
break-glass ``super_permission``) — so no live database (or running app) is
needed. Mirrors ``scripts/dump_erd.py``: deterministic output, skip-write on
identical content so the pre-push hook does not see a spurious change.
"""

from pathlib import Path

from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.roles import ELEVATED_ROLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "permissions.md"

HEADER = """# Roles & permissions matrix

Auto-generated from `app/core/auth/roles.py` and the object-role registry
(`app/core/auth/object_roles/registry.py`) by `make permissionsdump` — **do not
edit by hand**. Regenerate after any change to the canonical roles, permissions,
or object-scope policy and commit the result in the same diff (the pre-push hook
does this for you; CI fails on drift)."""


def _roles_table() -> str:
    lines = [
        "## Roles",
        "",
        "| Role | Display name | Description |",
        "| --- | --- | --- |",
    ]
    for role in SystemRole:
        spec = role.spec
        lines.append(f"| `{role.value}` | {spec.display_name} | {spec.description} |")
    return "\n".join(lines)


def _breakglass_cells() -> set[tuple[SystemRole, Permission]]:
    """`(role, permission)` pairs a role reaches only via an object scope's break-glass.

    A role holding a scope's `super_permission` has full control of every object
    of that type, so it effectively holds — at object scope — any permission the
    scope's *assignable* roles can hold. We mark only the gap: permissions
    reachable that way but absent from the role's own global spec (cells the flat
    matrix would otherwise show as an empty, misleading blank — e.g. `admin` and
    `evaluation_groups:manage_members`). Derived from the registry, never
    hardcoded.

    One-vocabulary caveat (see the registry module docstring): the union takes
    the assignable roles' *full* specs, so it can include platform-only
    permissions that gate nothing at object scope (e.g. `users:invite` via
    `owner`). Invisible today — every current `super_permission` holder also
    carries those globally — and consistent with the runtime rule that
    break-glass passes any object-scope check.
    """
    cells: set[tuple[SystemRole, Permission]] = set()
    for spec in OBJECT_ROLE_REGISTRY.values():
        if spec.super_permission is None:
            continue
        object_scope_permissions = {p for role in spec.assignable_system_roles for p in role.spec.permissions}
        for role in SystemRole:
            if spec.super_permission in role.spec.permissions:
                cells.update((role, p) for p in object_scope_permissions - role.spec.permissions)
    return cells


def _matrix_table() -> str:
    roles = list(SystemRole)
    header = "| Permission | " + " | ".join(f"`{r.value}`" for r in roles) + " |"
    divider = "| --- |" + " :-: |" * len(roles)
    lines = [
        "## Permission matrix",
        "",
        "Columns are roles. ✓ = granted globally (carried in the JWT); ○ = conferred"
        " only at object scope via a break-glass permission (see"
        " [Object scopes](#object-scopes)). Permissions are grouped by the resource"
        " they gate.",
        "",
        header,
        divider,
    ]

    granted = {role: {p.value for p in role.spec.permissions} for role in roles}
    breakglass = _breakglass_cells()
    current_resource: str | None = None
    for permission in Permission:
        resource = permission.value.split(":", 1)[0]
        if resource != current_resource:
            current_resource = resource
            lines.append(f"| **{resource}** |" + " |" * len(roles))
        cells = " | ".join(
            "✓" if permission.value in granted[r] else ("○" if (r, permission) in breakglass else "") for r in roles
        )
        lines.append(f"| `{permission.value}` | {cells} |")
    return "\n".join(lines)


def _object_scopes_table() -> str:
    lines = [
        "## Object scopes",
        "",
        "Some objects carry a *second*, per-object authorization scope (the"
        " `object_roles` layer). Roles assigned on a specific object are resolved"
        " per request — **not** carried in the JWT — and a held object role grants"
        " its own row from the matrix above, scoped to that object. The override"
        " rule: once a user holds any role on an object, those permissions are"
        " their *entire* effective authority on it (a global JWT permission grants"
        " no object-scope say); only the break-glass permission below survives it,"
        " conferring full control of every object of that type. A *protected role*,"
        " when set, must always retain at least one live holder — its last holder"
        " can be neither removed nor demoted (even by a break-glass admin), so the"
        " object can't be orphaned of that authority.",
        "",
        "| Object type | Assignable roles | Break-glass permission | Protected role |",
        "| --- | --- | --- | --- |",
    ]
    for object_type, spec in sorted(OBJECT_ROLE_REGISTRY.items(), key=lambda kv: kv[0].value):
        # Render assignable roles in SystemRole declaration order for stable output.
        assignable = ", ".join(f"`{role.value}`" for role in SystemRole if role in spec.assignable_system_roles)
        breakglass = f"`{spec.super_permission.value}`" if spec.super_permission else "—"
        protected = f"`{spec.protected_role.value}`" if spec.protected_role else "—"
        lines.append(f"| `{object_type.value}` | {assignable} | {breakglass} | {protected} |")
    return "\n".join(lines)


def _elevations_table() -> str:
    lines = [
        "## Role elevations",
        "",
        "Assigning *or* revoking these roles requires the listed permission, held in"
        " addition to the operation's own gate (see"
        " `services.users.assert_can_assign_roles`).",
        "",
        "| Role | Required permission |",
        "| --- | --- |",
    ]
    for role, permission in sorted(ELEVATED_ROLE_PERMISSIONS.items(), key=lambda kv: kv[0].value):
        lines.append(f"| `{role.value}` | `{permission.value}` |")
    return "\n".join(lines)


def render() -> str:
    sections = [HEADER, _roles_table(), _matrix_table(), _object_scopes_table(), _elevations_table()]
    return "\n\n".join(sections) + "\n"


def main() -> None:
    rendered = render()
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == rendered:
        return
    OUTPUT.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
