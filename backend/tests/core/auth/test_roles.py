"""Unit tests for the canonical role/permission definitions."""

import pytest

from app.core.auth.roles import ELEVATED_ROLE_PERMISSIONS
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import ROLE_SPECS
from app.core.auth.roles import Permission
from app.core.auth.roles import RoleSpec
from app.core.auth.roles import SystemRole


@pytest.mark.unit
def test_every_role_has_a_spec_and_permission_set() -> None:
    assert set(ROLE_SPECS) == set(SystemRole)
    for role in SystemRole:
        assert isinstance(role.spec, RoleSpec)
        assert role.spec.display_name
        assert role.spec.description
        assert role in ROLE_PERMISSIONS


@pytest.mark.unit
def test_spec_property_returns_the_registered_spec() -> None:
    assert SystemRole.RED_TEAMER.spec is ROLE_SPECS[SystemRole.RED_TEAMER]
    assert SystemRole.RED_TEAMER.spec.display_name == "Red Teamer"


@pytest.mark.unit
def test_admin_holds_user_management_and_manage_admin_permissions() -> None:
    admin_perms = ROLE_PERMISSIONS[SystemRole.ADMIN]
    assert {
        Permission.USERS_READ,
        Permission.USERS_UPDATE,
        Permission.USERS_DELETE,
        Permission.USERS_INVITE,
        Permission.USERS_MANAGE_ADMIN,
    } <= admin_perms


@pytest.mark.unit
def test_evaluation_approval_is_held_only_by_admin() -> None:
    # Minimal approval workflow: only admins approve/reject evaluations.
    for role in SystemRole:
        holds_approve = Permission.EVALUATIONS_APPROVE in role.spec.permissions
        assert holds_approve == (role is SystemRole.ADMIN)


@pytest.mark.unit
def test_owner_can_invite_but_not_manage_admin() -> None:
    owner_perms = ROLE_PERMISSIONS[SystemRole.OWNER]

    assert Permission.USERS_INVITE in owner_perms
    assert Permission.USERS_MANAGE_ADMIN not in owner_perms


@pytest.mark.unit
def test_owner_reads_models_but_does_not_manage_them() -> None:
    # Owners curate a group's allowed-model subset (models:read-gated) and see it
    # in the group detail; the model registry's create/update/delete stays admin-only.
    owner_perms = ROLE_PERMISSIONS[SystemRole.OWNER]
    management = {Permission.MODELS_CREATE, Permission.MODELS_UPDATE, Permission.MODELS_DELETE}
    assert Permission.MODELS_READ in owner_perms
    assert management.isdisjoint(owner_perms)
    assert management <= ROLE_PERMISSIONS[SystemRole.ADMIN]


@pytest.mark.unit
@pytest.mark.parametrize("role", [SystemRole.ADMIN, SystemRole.OWNER])
def test_management_roles_hold_full_evaluation_crud(role: SystemRole) -> None:
    # Admins and owners author and curate evaluations — they carry the full
    # create/update/delete set on top of read.
    assert {
        Permission.EVALUATIONS_READ,
        Permission.EVALUATIONS_CREATE,
        Permission.EVALUATIONS_UPDATE,
        Permission.EVALUATIONS_DELETE,
    } <= ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_admin_and_owner_can_create_and_edit_evaluation_groups() -> None:
    # Create/edit is held by admin + owner; the capability roles (red teamer /
    # annotator / viewer) stay read-only.
    create_edit = {Permission.EVALUATION_GROUPS_CREATE, Permission.EVALUATION_GROUPS_UPDATE}
    assert create_edit <= ROLE_PERMISSIONS[SystemRole.ADMIN]
    assert create_edit <= ROLE_PERMISSIONS[SystemRole.OWNER]
    for role in (SystemRole.RED_TEAMER, SystemRole.ANNOTATOR, SystemRole.VIEWER):
        assert create_edit.isdisjoint(ROLE_PERMISSIONS[role])


@pytest.mark.unit
def test_evaluation_groups_manage_is_admin_only() -> None:
    # `manage` lifts visibility/ownership globally; until per-instance scoping
    # exists only the (global) admin may hold it — owner is single-instance-scoped.
    for role in SystemRole:
        holds_manage = Permission.EVALUATION_GROUPS_MANAGE in role.spec.permissions
        assert holds_manage == (role is SystemRole.ADMIN)


@pytest.mark.unit
def test_elevated_role_permissions_gates_admin_and_owner() -> None:
    # Both behind the same non-delegable key: `users:invite` is delegable to a custom role,
    # so an ungated `owner` was assignable by one (and deletable via `users:delete`).
    assert ELEVATED_ROLE_PERMISSIONS == {
        SystemRole.ADMIN: Permission.USERS_MANAGE_ADMIN,
        SystemRole.OWNER: Permission.USERS_MANAGE_ADMIN,
    }


@pytest.mark.unit
def test_manage_admin_is_held_only_by_admin() -> None:
    # Guards the default-allow assignment check: granting an elevated role must
    # require its permission, and no non-admin role may carry it.
    for role in SystemRole:
        holds_manage_admin = Permission.USERS_MANAGE_ADMIN in role.spec.permissions
        assert holds_manage_admin == (role is SystemRole.ADMIN)


@pytest.mark.unit
def test_organizations_read_is_held_by_every_role() -> None:
    # Org read is broadly held — any role can list/view organizations.
    for role in SystemRole:
        assert Permission.ORGANIZATIONS_READ in ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_saved_views_crud_is_held_by_every_role() -> None:
    # Saved views are personal data; every role manages its own, so the full
    # owner-scoped CRUD set is broadly held (like `organizations:read`).
    crud = {
        Permission.SAVED_VIEWS_READ,
        Permission.SAVED_VIEWS_CREATE,
        Permission.SAVED_VIEWS_UPDATE,
        Permission.SAVED_VIEWS_DELETE,
    }
    for role in SystemRole:
        assert crud <= ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_notifications_read_update_is_held_by_every_role() -> None:
    # Notifications are personal data; every role reads and marks its own, so
    # both keys are broadly held (like the saved-views CRUD set).
    keys = {Permission.NOTIFICATIONS_READ, Permission.NOTIFICATIONS_UPDATE}
    for role in SystemRole:
        assert keys <= ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_organizations_management_is_admin_only() -> None:
    # Creating/editing/deleting organizations and assigning members is admin-only;
    # every other role is read-only on organizations.
    management = {
        Permission.ORGANIZATIONS_CREATE,
        Permission.ORGANIZATIONS_UPDATE,
        Permission.ORGANIZATIONS_DELETE,
        Permission.ORGANIZATIONS_MANAGE_MEMBERS,
    }
    assert management <= ROLE_PERMISSIONS[SystemRole.ADMIN]
    for role in SystemRole:
        if role is not SystemRole.ADMIN:
            assert management.isdisjoint(ROLE_PERMISSIONS[role])


@pytest.mark.unit
def test_viewer_carries_only_implemented_read_keys() -> None:
    # Reading evaluations, evaluation groups, and organizations are the only
    # implemented capabilities for the viewer. It does NOT carry
    # `view_personal_metrics` — it authors no submissions, so a personal-scope
    # dashboard would be empty; its metrics reach rides the wider levels. Its
    # remaining spec'd capabilities (dashboards / aggregated reads) are
    # unimplemented, so these keys plus the personal saved-views CRUD (held by
    # every role) are all it carries.
    assert ROLE_PERMISSIONS[SystemRole.VIEWER] == frozenset(
        {
            Permission.EVALUATIONS_READ,
            Permission.EVALUATION_GROUPS_READ,
            Permission.ORGANIZATIONS_READ,
            Permission.SAVED_VIEWS_READ,
            Permission.SAVED_VIEWS_CREATE,
            Permission.SAVED_VIEWS_UPDATE,
            Permission.SAVED_VIEWS_DELETE,
            Permission.NOTIFICATIONS_READ,
            Permission.NOTIFICATIONS_UPDATE,
        }
    )


@pytest.mark.unit
def test_annotator_carries_read_keys_plus_notes_flags_and_reviews() -> None:
    # The annotator reads evaluations/groups and message flags, holds the full
    # owner-scoped note CRUD (its core surface), and is the reviewer: full
    # review CRUD (assign / verdict / unassign). It holds no flag *write* key — it
    # reviews flagged outputs but authors none. Annotations are its second authoring
    # surface: shared reads plus create/delete, and no update key exists at all.
    assert ROLE_PERMISSIONS[SystemRole.ANNOTATOR] == frozenset(
        {
            Permission.EVALUATIONS_READ,
            Permission.EVALUATION_GROUPS_READ,
            Permission.ORGANIZATIONS_READ,
            Permission.NOTES_READ,
            Permission.NOTES_CREATE,
            Permission.NOTES_UPDATE,
            Permission.NOTES_DELETE,
            Permission.ANNOTATIONS_READ,
            Permission.ANNOTATIONS_CREATE,
            Permission.ANNOTATIONS_DELETE,
            Permission.FLAGS_READ,
            Permission.REVIEWS_READ,
            Permission.REVIEWS_CREATE,
            Permission.REVIEWS_UPDATE,
            Permission.REVIEWS_DELETE,
            Permission.REVIEWS_ANNOTATE,
            Permission.SAVED_VIEWS_READ,
            Permission.SAVED_VIEWS_CREATE,
            Permission.SAVED_VIEWS_UPDATE,
            Permission.SAVED_VIEWS_DELETE,
            Permission.NOTIFICATIONS_READ,
            Permission.NOTIFICATIONS_UPDATE,
        }
    )


@pytest.mark.unit
def test_red_teamer_carries_read_keys_plus_conversations() -> None:
    # The red teamer's implemented surface: the two read keys, full owner-scoped
    # conversation CRUD (their own attempts), participation (send a message /
    # stream a reply), full owner-scoped message-flag CRUD, review *read*
    # (verdicts on their own flags), and `view_personal_metrics` — which, at the
    # `members_personal_metrics` level, gives them a personal-scope metrics read.
    # No conversation key is held by annotator / viewer yet.
    assert ROLE_PERMISSIONS[SystemRole.RED_TEAMER] == frozenset(
        {
            Permission.EVALUATIONS_READ,
            Permission.EVALUATION_GROUPS_READ,
            Permission.EVALUATION_GROUPS_VIEW_PERSONAL_METRICS,
            Permission.ORGANIZATIONS_READ,
            Permission.CONVERSATIONS_READ,
            Permission.CONVERSATIONS_CREATE,
            Permission.CONVERSATIONS_UPDATE,
            Permission.CONVERSATIONS_DELETE,
            Permission.CONVERSATIONS_PARTICIPATE,
            Permission.FLAGS_READ,
            Permission.FLAGS_CREATE,
            Permission.FLAGS_UPDATE,
            Permission.FLAGS_DELETE,
            Permission.NOTES_READ,
            Permission.NOTES_CREATE,
            Permission.NOTES_UPDATE,
            Permission.NOTES_DELETE,
            Permission.REVIEWS_READ,
            Permission.SAVED_VIEWS_READ,
            Permission.SAVED_VIEWS_CREATE,
            Permission.SAVED_VIEWS_UPDATE,
            Permission.SAVED_VIEWS_DELETE,
            Permission.NOTIFICATIONS_READ,
            Permission.NOTIFICATIONS_UPDATE,
        }
    )
    for role in (SystemRole.ANNOTATOR, SystemRole.VIEWER):
        assert Permission.CONVERSATIONS_READ not in ROLE_PERMISSIONS[role]
        assert Permission.CONVERSATIONS_PARTICIPATE not in ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_annotator_can_create_notes() -> None:
    # The target user: an annotator attaches notes to model responses.
    assert Permission.NOTES_CREATE in ROLE_PERMISSIONS[SystemRole.ANNOTATOR]


@pytest.mark.unit
def test_annotator_can_edit_own_notes() -> None:
    # Authoring without edit/delete would leave an annotator unable to fix a typo in
    # their own note. These are note keys, not flag keys — an annotator has no
    # authority over message flags beyond reading them.
    assert {Permission.NOTES_UPDATE, Permission.NOTES_DELETE} <= ROLE_PERMISSIONS[SystemRole.ANNOTATOR]
    assert {Permission.FLAGS_UPDATE, Permission.FLAGS_DELETE}.isdisjoint(ROLE_PERMISSIONS[SystemRole.ANNOTATOR])


@pytest.mark.unit
@pytest.mark.parametrize("role", [SystemRole.VIEWER, SystemRole.OWNER])
def test_roles_without_note_authority_hold_no_note_key(role: SystemRole) -> None:
    # `owner` is deliberately excluded: reads are author-scoped, so a note key
    # alone would hand it an endpoint that always returns an empty page.
    assert {p for p in ROLE_PERMISSIONS[role] if str(p).startswith("notes:")} == set()


@pytest.mark.unit
@pytest.mark.parametrize("role", list(SystemRole))
@pytest.mark.parametrize("write_key", [Permission.NOTES_CREATE, Permission.NOTES_UPDATE, Permission.NOTES_DELETE])
def test_note_write_keys_imply_notes_read(role: SystemRole, write_key: Permission) -> None:
    # A role that can author but not read would write notes it can never see again.
    # `update` and `delete` are held to the same rule because `PATCH` returns the full
    # body under `notes:update` alone, so a role holding it without `read` would
    # read a note through the write verb.
    # Parametrized over every role, not just the holders: filtering at collection time
    # would turn a lost grant into zero cases — a skip, not a failure.
    if write_key in ROLE_PERMISSIONS[role]:
        assert Permission.NOTES_READ in ROLE_PERMISSIONS[role]


@pytest.mark.unit
def test_note_keys_are_granted_to_the_intended_roles() -> None:
    """Pins both directions of the split, so a stray grant fails rather than passing silently."""
    holders = {role for role in SystemRole if Permission.NOTES_CREATE in ROLE_PERMISSIONS[role]}
    assert holders == {SystemRole.ADMIN, SystemRole.RED_TEAMER, SystemRole.ANNOTATOR}


@pytest.mark.unit
def test_annotation_keys_are_granted_to_the_intended_roles() -> None:
    """Pins the surprising half: the red teamer gets no annotation key at all.

    The inverse of the note split, where the red teamer holds the full CRUD and the owner
    nothing — reads here are not author-scoped, so an owner sees its groups' annotations
    without being able to author any.
    """
    readers = {role for role in SystemRole if Permission.ANNOTATIONS_READ in ROLE_PERMISSIONS[role]}
    writers = {role for role in SystemRole if Permission.ANNOTATIONS_CREATE in ROLE_PERMISSIONS[role]}
    deleters = {role for role in SystemRole if Permission.ANNOTATIONS_DELETE in ROLE_PERMISSIONS[role]}

    assert readers == {SystemRole.ADMIN, SystemRole.OWNER, SystemRole.ANNOTATOR}
    assert writers == deleters == {SystemRole.ADMIN, SystemRole.ANNOTATOR}


@pytest.mark.unit
def test_no_annotation_update_key_is_minted() -> None:
    # A tag has no editable content, so changing the label is delete + re-create. Asserted
    # against the vocabulary, not one role, so nobody can add the key and grant it quietly.
    assert not any(str(p) == "annotations:update" for p in Permission)
