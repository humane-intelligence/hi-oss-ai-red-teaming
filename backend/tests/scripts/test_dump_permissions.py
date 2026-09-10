"""Unit tests for `scripts.dump_permissions`.

`render()` is pure introspection of the canonical RBAC vocabulary in
`app.core.auth.roles`, so the assertions run against the real role/permission
definitions — the same source `sync_system_roles` projects onto the database.
"""

import os
from pathlib import Path

import pytest

from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from scripts import dump_permissions


def _matrix_rows(rendered: str) -> dict[str, str]:
    """First-column slug → full row, for every table line whose first cell is a backticked slug."""
    return {line.split("|")[1].strip().strip("`"): line for line in rendered.splitlines() if line.startswith("| `")}


@pytest.mark.unit
def test_render_is_deterministic() -> None:
    assert dump_permissions.render() == dump_permissions.render()


@pytest.mark.unit
def test_render_lists_every_role_with_its_display_name() -> None:
    rendered = dump_permissions.render()

    for role in SystemRole:
        assert f"| `{role.value}` | {role.spec.display_name} |" in rendered


@pytest.mark.unit
def test_render_includes_every_permission_as_a_matrix_row() -> None:
    rendered = dump_permissions.render()

    for permission in Permission:
        assert f"| `{permission.value}` |" in rendered


@pytest.mark.unit
def test_matrix_marks_only_granted_cells() -> None:
    rendered = dump_permissions.render()
    rows = _matrix_rows(rendered)

    roles = list(SystemRole)
    for permission in Permission:
        cells = [c.strip() for c in rows[permission.value].split("|")[2:-1]]
        for role, cell in zip(roles, cells, strict=True):
            granted = permission in role.spec.permissions
            assert (cell == "✓") == granted, f"{role.value} / {permission.value}"


@pytest.mark.unit
def test_break_glass_cells_carry_the_object_scope_marker() -> None:
    rendered = dump_permissions.render()
    rows = _matrix_rows(rendered)

    roles = list(SystemRole)
    for role, permission in dump_permissions._breakglass_cells():
        cells = [c.strip() for c in rows[permission.value].split("|")[2:-1]]
        assert cells[roles.index(role)] == "○", f"{role.value} / {permission.value}"


@pytest.mark.unit
def test_admin_reaches_manage_members_only_via_break_glass() -> None:
    # Guards the derivation against silently going empty: admin lacks this permission globally.
    assert (SystemRole.ADMIN, Permission.EVALUATION_GROUPS_MANAGE_MEMBERS) in dump_permissions._breakglass_cells()


@pytest.mark.unit
def test_object_scopes_table_lists_every_registered_type() -> None:
    rendered = dump_permissions.render()
    # Pin to the object type's own row — these slugs recur elsewhere in the doc, so doc-wide asserts are vacuous.
    rows = _matrix_rows(rendered)

    for object_type, spec in OBJECT_ROLE_REGISTRY.items():
        row = rows[object_type.value]
        for role in spec.assignable_system_roles:
            assert f"`{role.value}`" in row, f"{object_type.value} / {role.value}"
        # Columns: | type | assignable roles | break-glass | protected role |
        cells = [c.strip() for c in row.split("|")[1:-1]]
        expected_breakglass = f"`{spec.super_permission.value}`" if spec.super_permission else "—"
        expected_protected = f"`{spec.protected_role.value}`" if spec.protected_role else "—"
        assert cells[-2] == expected_breakglass, object_type.value
        assert cells[-1] == expected_protected, object_type.value


@pytest.mark.unit
def test_main_writes_rendered_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "permissions.md"
    monkeypatch.setattr(dump_permissions, "OUTPUT", output)

    dump_permissions.main()

    assert output.read_text(encoding="utf-8") == dump_permissions.render()


@pytest.mark.unit
def test_main_skips_write_on_identical_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "permissions.md"
    output.write_text(dump_permissions.render(), encoding="utf-8")
    os.utime(output, (0, 0))  # sentinel mtime — a rewrite would bump it
    monkeypatch.setattr(dump_permissions, "OUTPUT", output)

    dump_permissions.main()

    assert output.stat().st_mtime == 0
