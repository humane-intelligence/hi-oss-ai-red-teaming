"""Canonical system roles, permissions, and the invite-elevation map.

Single source of truth for the platform's RBAC vocabulary. `sync_system_roles`
(`app/core/auth/services/roles.py`) projects these definitions onto `roles`
rows on every deploy; `require_permission` (`app/core/auth/dependencies.py`)
gates routes on the permission strings carried in the session JWT.

Only permissions backing *implemented* functionality live here. Capabilities the
product spec assigns to a role but whose mechanism is missing or only partly built —
instance-scoped evaluation authority, evaluation assignment, dashboards — are recorded
as ``# TODO(<area>)`` comments beside the role, so the intent is captured without
minting dead permission keys.
"""

from dataclasses import dataclass
from enum import StrEnum


class SystemRole(StrEnum):
    """Canonical platform roles.

    The value is the stable slug stored in `roles.name` and used as the machine
    identifier (lookups, `ELEVATED_ROLE_PERMISSIONS` keys) — never a
    human-facing label (that lives in `RoleSpec.display_name`).
    """

    ADMIN = "admin"
    OWNER = "owner"
    RED_TEAMER = "red_teamer"
    ANNOTATOR = "annotator"
    VIEWER = "viewer"

    @property
    def spec(self) -> RoleSpec:
        """Descriptive config for this role (display name, description, permissions).

        Total by contract — every canonical role has a `RoleSpec`. A missing
        entry is a misconfiguration and raises `KeyError` rather than returning
        `None`, so the bug surfaces loudly at first access instead of spreading
        optionality through every call site.
        """
        return ROLE_SPECS[self]


# The base global role every new participant account lands on — self-signup and
# admin/owner-issued invitations alike. An admin/owner elevates later via
# PATCH /users or a fresh invitation. Seeds the `is_default` / `is_participant_default`
# flags on first sync; the flags (operator-reassignable) are the runtime source of truth.
DEFAULT_PARTICIPANT_ROLE: SystemRole = SystemRole.RED_TEAMER


class Permission(StrEnum):
    """Permission keys backing implemented functionality."""

    USERS_READ = "users:read"
    USERS_UPDATE = "users:update"
    USERS_DELETE = "users:delete"
    # Coarse gate: required to issue any invitation (checked at the route).
    USERS_INVITE = "users:invite"
    # Elevation: required to assign or revoke the admin role, and to delete a
    # user who currently holds it. Held in addition to the operation's own gate.
    USERS_MANAGE_ADMIN = "users:manage_admin"
    # Force-logout: revoke all of a user's active sessions (single + bulk).
    USERS_MANAGE_SESSIONS = "users:manage_sessions"

    # Read the role + permission catalogs (`GET /roles`, `GET /permissions`) —
    # the source the admin/owner role-assignment UI lists. Held by everyone who
    # assigns roles: admin (user create/update) and owner (invitations).
    ROLES_READ = "roles:read"
    # Create/edit/activate/delete roles (the role-management surface). Admin-only
    # and non-delegable (see NON_DELEGABLE_PERMISSIONS): it's an elevation vector,
    # so it can never be granted through a custom role's permission set.
    ROLES_MANAGE = "roles:manage"

    # Organizations are the tenancy root. `read` is held by every role; the
    # management split (create/update/delete/manage_members) is admin-only —
    # platform admins create orgs and assign/unassign users.
    ORGANIZATIONS_READ = "organizations:read"
    ORGANIZATIONS_CREATE = "organizations:create"
    ORGANIZATIONS_UPDATE = "organizations:update"
    ORGANIZATIONS_DELETE = "organizations:delete"
    ORGANIZATIONS_MANAGE_MEMBERS = "organizations:manage_members"

    MODELS_READ = "models:read"
    MODELS_CREATE = "models:create"
    MODELS_UPDATE = "models:update"
    MODELS_DELETE = "models:delete"

    EVALUATIONS_READ = "evaluations:read"
    EVALUATIONS_CREATE = "evaluations:create"
    EVALUATIONS_UPDATE = "evaluations:update"
    EVALUATIONS_DELETE = "evaluations:delete"
    EVALUATIONS_APPROVE = "evaluations:approve"

    EVALUATION_GROUPS_READ = "evaluation_groups:read"
    # Object-scoped: manage a group's membership (assign/replace/remove roles).
    # Conferred by the in-group `owner` role via the object-role registry
    # (`app/core/auth/object_roles/`); resolved per request from the caller's
    # object-role assignments, never carried in the JWT.
    EVALUATION_GROUPS_MANAGE_MEMBERS = "evaluation_groups:manage_members"
    EVALUATION_GROUPS_CREATE = "evaluation_groups:create"
    EVALUATION_GROUPS_UPDATE = "evaluation_groups:update"
    # Object-scoped: view a group's whole-event aggregate metrics (the reporting
    # dashboard). Distinct from `:update` so a read-only reporting role can be
    # granted it without edit rights; conferred by the in-group `owner` role today
    # and lifted by the `evaluation_groups:manage` break-glass.
    EVALUATION_GROUPS_VIEW_METRICS = "evaluation_groups:view_metrics"
    # Object-scoped: at the `members_personal_metrics` access level, admits a member
    # to the metrics dashboards *scoped to their own contributions* (their
    # submissions, conversations, and the reviews of their submissions). A gating
    # permission the metrics gate reads off the member's in-group roles — held by
    # the `red_teamer` role, so a future custom role opts in by carrying it, never
    # by its name. Distinct from `view_metrics`, the always-pass capability that
    # sees the *full* event-wide dashboards (owner / break-glass): this one never
    # bypasses the configured level and never widens beyond the holder's own data.
    EVALUATION_GROUPS_VIEW_PERSONAL_METRICS = "evaluation_groups:view_personal_metrics"
    # Object-scoped: run and download a group's CSV exports. The bundle carries every
    # member's transcripts, flags and reviews, so it is granted deliberately rather
    # than inferred from edit rights.
    EVALUATION_GROUPS_EXPORT = "evaluation_groups:export"
    # Break-glass elevation carried in the JWT: bypasses the per-object override
    # so an admin retains full control of any group even when assigned a lesser
    # in-group role. Also lifts the list/get visibility scope and the
    # in-group-role requirement on writes.
    EVALUATION_GROUPS_MANAGE = "evaluation_groups:manage"

    CONVERSATIONS_READ = "conversations:read"
    CONVERSATIONS_CREATE = "conversations:create"
    CONVERSATIONS_UPDATE = "conversations:update"
    CONVERSATIONS_DELETE = "conversations:delete"
    # Object-scoped: lifts the owner predicate on conversation *reads* for a group the
    # caller holds it on, so a group owner sees its members' transcripts. Reads only —
    # update and delete stay owner-scoped for every holder.
    CONVERSATIONS_READ_ANY = "conversations:read_any"
    # Coarse RBAC gate for talking to a model (send a message / stream a reply).
    # The stateless /chat/stream endpoint resolves no object, so this is its only
    # check; the evaluation-bound write-path layers an object-scope gate on top.
    CONVERSATIONS_PARTICIPATE = "conversations:participate"

    # Message flags — a red-teamer marks a model response as exploit-worthy.
    # Owner-scoped CRUD (the flag's author); the `evaluation_groups:manage`
    # break-glass lifts the owner scope. `flags:review` (the reviewer verdict
    # workflow) is deferred to a separate scope.
    FLAGS_READ = "flags:read"
    FLAGS_CREATE = "flags:create"
    FLAGS_UPDATE = "flags:update"
    FLAGS_DELETE = "flags:delete"

    # Notes — an annotator's free-text remark on a selection of one conversation's
    # messages, independent of whether a participant flagged it. A full own split
    # rather than riding `flags:*`: authoring reaches conversations the caller does
    # not own, so the two entities do not share authority, and the permissions matrix
    # would otherwise credit an annotator with editing *flags*. Reads stay
    # owner-scoped in the service; `evaluation_groups:manage` is the break-glass.
    NOTES_READ = "notes:read"
    NOTES_CREATE = "notes:create"
    NOTES_UPDATE = "notes:update"
    NOTES_DELETE = "notes:delete"

    # Annotations — a per-message label, picked from the shared vocabulary or typed ad hoc.
    # A separate split from `notes:*` because they are different things: a note is prose
    # about a selection of messages, an annotation is a label you aggregate over. Reads are
    # deliberately *not* author-scoped, unlike notes — `read` also gates the label
    # vocabulary. No `annotations:update` on purpose: the row has no editable content, so
    # changing the label is delete + re-create.
    ANNOTATIONS_READ = "annotations:read"
    ANNOTATIONS_CREATE = "annotations:create"
    ANNOTATIONS_DELETE = "annotations:delete"

    # Reviews — a reviewer's verdict on a flagged submission (`MessageFlag`).
    # Assigning a reviewer, recording a verdict, and unassigning are held by
    # annotator / owner / admin; the red-teamer holds read only (scoped by the
    # service to their own flags' reviews). The `evaluation_groups:manage`
    # break-glass lifts the read/write scope for admin.
    REVIEWS_READ = "reviews:read"
    REVIEWS_CREATE = "reviews:create"
    REVIEWS_UPDATE = "reviews:update"
    REVIEWS_DELETE = "reviews:delete"
    REVIEWS_ANNOTATE = "reviews:annotate"

    # Platform-wide settings singleton (the default data license today). Admin-only:
    # read the current default and override it. Distinct from the code license.
    PLATFORM_SETTINGS_READ = "platform_settings:read"
    PLATFORM_SETTINGS_UPDATE = "platform_settings:update"
    # Data licenses: read is open (the picker), so no `licenses:read`. `manage` is the admin-only
    # break-glass that lifts the owner's own-only / curated-locked edit scope.
    LICENSES_CREATE = "licenses:create"
    LICENSES_UPDATE = "licenses:update"
    LICENSES_DELETE = "licenses:delete"
    LICENSES_MANAGE = "licenses:manage"

    # Audit log — read the append-only trail of sensitive actions/accesses. Admin-only.
    AUDIT_READ = "audit:read"

    # Saved views — a user's named filter/sort/column state for any list view.
    # Personal data: every role holds the full owner-scoped CRUD set (like
    # `organizations:read`), so anyone can manage their own views.
    SAVED_VIEWS_READ = "saved_views:read"
    SAVED_VIEWS_CREATE = "saved_views:create"
    SAVED_VIEWS_UPDATE = "saved_views:update"
    SAVED_VIEWS_DELETE = "saved_views:delete"

    # Notifications — a user's in-app notification feed. Personal data: every role
    # holds read + update (mark read/unread) of their own, like saved views. No
    # create/delete: rows are minted internally and there is no user-facing delete.
    NOTIFICATIONS_READ = "notifications:read"
    NOTIFICATIONS_UPDATE = "notifications:update"


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """Intrinsic, self-contained config for one canonical role.

    `permissions` is the complete set granted by the role; `sync_system_roles`
    writes it verbatim into `roles.permissions`.
    """

    display_name: str
    description: str
    permissions: frozenset[Permission]


ROLE_SPECS: dict[SystemRole, RoleSpec] = {
    SystemRole.ADMIN: RoleSpec(
        display_name="Admin",
        description="Full administrative access: manage users and invite any role.",
        permissions=frozenset(
            {
                Permission.USERS_READ,
                Permission.USERS_UPDATE,
                Permission.USERS_DELETE,
                Permission.USERS_INVITE,
                Permission.USERS_MANAGE_ADMIN,
                Permission.USERS_MANAGE_SESSIONS,
                Permission.ROLES_READ,
                Permission.ROLES_MANAGE,
                # Organizations: admin holds the full set — `read` (shared with every
                # role) plus the admin-only create/update/delete/manage_members.
                Permission.ORGANIZATIONS_READ,
                Permission.ORGANIZATIONS_CREATE,
                Permission.ORGANIZATIONS_UPDATE,
                Permission.ORGANIZATIONS_DELETE,
                Permission.ORGANIZATIONS_MANAGE_MEMBERS,
                Permission.MODELS_READ,
                Permission.MODELS_CREATE,
                Permission.MODELS_UPDATE,
                Permission.MODELS_DELETE,
                Permission.EVALUATIONS_READ,
                Permission.EVALUATIONS_CREATE,
                Permission.EVALUATIONS_UPDATE,
                Permission.EVALUATIONS_DELETE,
                Permission.EVALUATIONS_APPROVE,
                Permission.EVALUATION_GROUPS_READ,
                Permission.EVALUATION_GROUPS_CREATE,
                Permission.EVALUATION_GROUPS_UPDATE,
                Permission.EVALUATION_GROUPS_VIEW_METRICS,
                # Break-glass: this global permission is the registered
                # `super_permission` for the evaluation-group object scope, so it
                # bypasses the per-object override and grants admin full in-group
                # control (incl. manage_members) without an explicit assignment.
                Permission.EVALUATION_GROUPS_MANAGE,
                # Conversations are owner-scoped; admin holds the keys plus the
                # `evaluation_groups:manage` break-glass that lifts the owner scope,
                # so it can read/manage any user's conversation.
                Permission.CONVERSATIONS_READ,
                Permission.CONVERSATIONS_CREATE,
                Permission.CONVERSATIONS_UPDATE,
                Permission.CONVERSATIONS_DELETE,
                Permission.CONVERSATIONS_PARTICIPATE,
                # Message flags: admin holds the keys, plus the
                # `evaluation_groups:manage` break-glass that lifts the owner
                # scope so it can read/manage any user's flag.
                Permission.FLAGS_READ,
                Permission.FLAGS_CREATE,
                Permission.FLAGS_UPDATE,
                Permission.FLAGS_DELETE,
                # Notes: admin holds the keys, plus the `evaluation_groups:manage`
                # break-glass that lifts the author scope to any user's note.
                Permission.NOTES_READ,
                Permission.NOTES_CREATE,
                Permission.NOTES_UPDATE,
                Permission.NOTES_DELETE,
                # Annotations: create/delete plus shared reads; the author scope on
                # delete is lifted by the `evaluation_groups:manage` break-glass.
                Permission.ANNOTATIONS_READ,
                Permission.ANNOTATIONS_CREATE,
                Permission.ANNOTATIONS_DELETE,
                # Reviews: admin holds the keys, plus the `evaluation_groups:manage`
                # break-glass that lifts the read/write scope to any group's reviews.
                Permission.REVIEWS_READ,
                Permission.REVIEWS_CREATE,
                Permission.REVIEWS_UPDATE,
                Permission.REVIEWS_DELETE,
                # Platform settings: only the admin reads/sets the platform default
                # data license.
                Permission.PLATFORM_SETTINGS_READ,
                Permission.PLATFORM_SETTINGS_UPDATE,
                Permission.LICENSES_CREATE,
                Permission.LICENSES_UPDATE,
                Permission.LICENSES_DELETE,
                Permission.LICENSES_MANAGE,
                # Audit log: only the admin reads the trail.
                Permission.AUDIT_READ,
                # Saved views: personal list state — full owner-scoped CRUD.
                Permission.SAVED_VIEWS_READ,
                Permission.SAVED_VIEWS_CREATE,
                Permission.SAVED_VIEWS_UPDATE,
                Permission.SAVED_VIEWS_DELETE,
                # Notifications: personal feed — read + mark read/unread.
                Permission.NOTIFICATIONS_READ,
                Permission.NOTIFICATIONS_UPDATE,
            },
        ),
    ),
    SystemRole.OWNER: RoleSpec(
        display_name="Owner",
        description="Manages a single instance and invites owners, red teamers, annotators, and viewers.",
        # No `evaluation_groups:manage`: that elevation is global (read/edit ANY
        # group), which over-reaches an owner's single-instance scope while no
        # instance model exists. The owner edits only its own groups for now.
        # TODO(evaluations): + evaluation_groups:manage (instance-scoped)
        # TODO(evaluations): + evaluations:approve for the owned instance.
        permissions=frozenset(
            {
                Permission.USERS_INVITE,
                # Reads the role catalog to populate the invitation role picker.
                Permission.ROLES_READ,
                Permission.EVALUATIONS_READ,
                Permission.EVALUATIONS_CREATE,
                Permission.EVALUATIONS_UPDATE,
                Permission.EVALUATIONS_DELETE,
                Permission.EVALUATION_GROUPS_READ,
                # Read the model registry to curate a group's allowed-model subset
                # (read-only; model CRUD stays admin-only).
                Permission.MODELS_READ,
                # Organizations: read-only — management is admin-only.
                Permission.ORGANIZATIONS_READ,
                Permission.EVALUATION_GROUPS_CREATE,
                Permission.EVALUATION_GROUPS_UPDATE,
                Permission.EVALUATION_GROUPS_VIEW_METRICS,
                Permission.EVALUATION_GROUPS_MANAGE_MEMBERS,
                Permission.EVALUATION_GROUPS_EXPORT,
                # Data licenses: curate own (no `manage` — curated + others' stay admin-only).
                Permission.LICENSES_CREATE,
                Permission.LICENSES_UPDATE,
                Permission.LICENSES_DELETE,
                Permission.CONVERSATIONS_PARTICIPATE,
                # Reads the transcripts of the groups it owns: the route gate plus the
                # object-scope lift that widens the owner predicate past its own rows.
                Permission.CONVERSATIONS_READ,
                Permission.CONVERSATIONS_READ_ANY,
                # Annotations: an owner will read the labels left on its groups' messages but
                # author none — unlike notes, which it cannot even read. Today the key reaches
                # only the label vocabulary; the annotation reads land with the entity.
                Permission.ANNOTATIONS_READ,
                # Reviews: an owner assigns reviewers and records verdicts within its groups.
                Permission.REVIEWS_READ,
                Permission.REVIEWS_CREATE,
                Permission.REVIEWS_UPDATE,
                Permission.REVIEWS_DELETE,
                # Saved views: personal list state — full owner-scoped CRUD.
                Permission.SAVED_VIEWS_READ,
                Permission.SAVED_VIEWS_CREATE,
                Permission.SAVED_VIEWS_UPDATE,
                Permission.SAVED_VIEWS_DELETE,
                # Notifications: personal feed — read + mark read/unread.
                Permission.NOTIFICATIONS_READ,
                Permission.NOTIFICATIONS_UPDATE,
            },
        ),
    ),
    SystemRole.RED_TEAMER: RoleSpec(
        display_name="Red Teamer",
        description="Participates as a red teamer in assigned evaluations.",
        # TODO(evaluations): + evaluations:participate once evaluation assignment exists.
        # Conversations are the red-teamer's own attempts: full owner-scoped CRUD, and
        # the owner predicate is never lifted for it (no `conversations:read_any`).
        permissions=frozenset(
            {
                Permission.EVALUATIONS_READ,
                Permission.EVALUATION_GROUPS_READ,
                # At the `members_personal_metrics` access level, sees the metrics
                # dashboards scoped to their own submissions/conversations/reviews.
                Permission.EVALUATION_GROUPS_VIEW_PERSONAL_METRICS,
                # Organizations: read-only — management is admin-only.
                Permission.ORGANIZATIONS_READ,
                Permission.CONVERSATIONS_READ,
                Permission.CONVERSATIONS_CREATE,
                Permission.CONVERSATIONS_UPDATE,
                Permission.CONVERSATIONS_DELETE,
                Permission.CONVERSATIONS_PARTICIPATE,
                # Message flags are the red-teamer's own: full owner-scoped CRUD.
                Permission.FLAGS_READ,
                Permission.FLAGS_CREATE,
                Permission.FLAGS_UPDATE,
                Permission.FLAGS_DELETE,
                # Notes: a red-teamer may also leave a note — a lighter remark beside
                # the flag. Authoring needs no ownership of the conversation, so this
                # reaches any transcript in a group the red-teamer can see; read,
                # update and delete stay scoped to the notes they authored.
                Permission.NOTES_READ,
                Permission.NOTES_CREATE,
                Permission.NOTES_UPDATE,
                Permission.NOTES_DELETE,
                # Reviews: read-only — a red-teamer sees verdicts on their own flags
                # (the service scopes reads to flags they authored).
                Permission.REVIEWS_READ,
                # Saved views: personal list state — full owner-scoped CRUD.
                Permission.SAVED_VIEWS_READ,
                Permission.SAVED_VIEWS_CREATE,
                Permission.SAVED_VIEWS_UPDATE,
                Permission.SAVED_VIEWS_DELETE,
                # Notifications: personal feed — read + mark read/unread.
                Permission.NOTIFICATIONS_READ,
                Permission.NOTIFICATIONS_UPDATE,
            },
        ),
    ),
    SystemRole.ANNOTATOR: RoleSpec(
        display_name="Annotator",
        description="Annotates conversations and evaluation outputs.",
        permissions=frozenset(
            {
                Permission.EVALUATIONS_READ,
                Permission.EVALUATION_GROUPS_READ,
                # Organizations: read-only — management is admin-only.
                Permission.ORGANIZATIONS_READ,
                # Notes: the annotator's prose surface — authoring needs no
                # ownership of the conversation, and read/update/delete stay
                # owner-scoped in the service.
                Permission.NOTES_READ,
                Permission.NOTES_CREATE,
                Permission.NOTES_UPDATE,
                Permission.NOTES_DELETE,
                # Annotations: the annotator's labelling surface — reads are shared,
                # authoring needs no ownership of the conversation.
                Permission.ANNOTATIONS_READ,
                Permission.ANNOTATIONS_CREATE,
                Permission.ANNOTATIONS_DELETE,
                # Message flags: read-only — it reviews flagged outputs but authors none.
                Permission.FLAGS_READ,
                # Reviews: the annotator is the reviewer — assignable pool, assign, record verdict, unassign.
                Permission.REVIEWS_READ,
                Permission.REVIEWS_CREATE,
                Permission.REVIEWS_UPDATE,
                Permission.REVIEWS_DELETE,
                Permission.REVIEWS_ANNOTATE,
                # Saved views: personal list state — full owner-scoped CRUD.
                Permission.SAVED_VIEWS_READ,
                Permission.SAVED_VIEWS_CREATE,
                Permission.SAVED_VIEWS_UPDATE,
                Permission.SAVED_VIEWS_DELETE,
                # Notifications: personal feed — read + mark read/unread.
                Permission.NOTIFICATIONS_READ,
                Permission.NOTIFICATIONS_UPDATE,
            },
        ),
    ),
    SystemRole.VIEWER: RoleSpec(
        display_name="Viewer",
        description="Views aggregated evaluation results and dashboards.",
        # TODO(analytics): + dashboards:read / evaluations:read_aggregated once dashboards exist.
        # No `view_personal_metrics`: a viewer authors no submissions/conversations,
        # so a personal-scoped dashboard would be empty. Its metrics reach rides the
        # wider `all_members` / `inherit_group_access` levels, not the personal one.
        permissions=frozenset(
            {
                Permission.EVALUATIONS_READ,
                Permission.EVALUATION_GROUPS_READ,
                Permission.ORGANIZATIONS_READ,
                # Saved views: personal list state — full owner-scoped CRUD.
                Permission.SAVED_VIEWS_READ,
                Permission.SAVED_VIEWS_CREATE,
                Permission.SAVED_VIEWS_UPDATE,
                Permission.SAVED_VIEWS_DELETE,
                # Notifications: personal feed — read + mark read/unread.
                Permission.NOTIFICATIONS_READ,
                Permission.NOTIFICATIONS_UPDATE,
            },
        ),
    ),
}


# Roles whose assignment *or removal* requires an elevated permission. The check is
# *default-allow*: any role NOT listed here is freely assignable/removable by any
# caller already authorized for the operation, so a newly added sensitive *system*
# role MUST be added here to keep it gated. Both entries key on `users:manage_admin`
# precisely because it is non-delegable — that is what stops an operator-defined role
# from minting admins or owners through `users:invite` / `users:update`. Custom roles
# can never be listed here (the map is `SystemRole`-keyed); the bound on what they may
# carry is `NON_DELEGABLE_PERMISSIONS`. Consumed by
# `services.users.assert_can_assign_roles` (invitation, user-create, user-update) and
# `assert_can_delete_user`.
ELEVATED_ROLE_PERMISSIONS: dict[SystemRole, Permission] = {
    SystemRole.ADMIN: Permission.USERS_MANAGE_ADMIN,
    SystemRole.OWNER: Permission.USERS_MANAGE_ADMIN,
}


# Permissions an operator can never fold into a custom role's set: each is an
# elevation vector — grant yourself role-management, the admin-assignment key, or
# (via `users:update`, which replaces a user's whole role set) any role that
# `ELEVATED_ROLE_PERMISSIONS` doesn't gate. System roles still hold them;
# `create_role`/`update_role` reject them in operator-defined sets. Extend as new
# elevation permissions land.
NON_DELEGABLE_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.ROLES_MANAGE, Permission.USERS_MANAGE_ADMIN, Permission.USERS_UPDATE}
)


# System roles that can never be deactivated. `admin` alone holds `roles:manage`,
# so switching it off would strip the platform of its only role manager
# (self-lockout); `owner` is stamped on every evaluation-group creator, so an
# inactive owner would grant a powerless in-group role. The default role is
# protected too, but dynamically via `is_default` — not by name. `annotator` and
# `viewer` are freely deactivatable.
NON_DEACTIVATABLE_SYSTEM_ROLES: frozenset[SystemRole] = frozenset({SystemRole.ADMIN, SystemRole.OWNER})


# Slugs reserved for canonical roles — a custom role can't claim one. The
# partial-unique index on `name` also blocks a live duplicate, but this rejects
# the collision with a clear 4xx before hitting the DB.
RESERVED_ROLE_NAMES: frozenset[str] = frozenset(role.value for role in SystemRole)


# Permission strings per role, flattened from each role's spec — what
# `sync_system_roles` writes into `roles.permissions`.
ROLE_PERMISSIONS: dict[SystemRole, frozenset[str]] = {
    role: frozenset(permission.value for permission in role.spec.permissions) for role in SystemRole
}


# Human-readable, one-line description per permission — the catalog the
# `GET /permissions` endpoint serves for the role-builder/review UI. Total by
# contract: a test asserts every `Permission` member has an entry, so a new
# permission can't ship without its description.
PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.USERS_READ: "View user accounts.",
    Permission.USERS_UPDATE: "Edit user accounts and their role assignments.",
    Permission.USERS_DELETE: "Delete (soft-delete) user accounts.",
    Permission.USERS_INVITE: "Issue platform invitations to new users.",
    Permission.USERS_MANAGE_ADMIN: "Assign, revoke, or delete the admin role (elevation).",
    Permission.USERS_MANAGE_SESSIONS: "Force-logout users (revoke all their active sessions).",
    Permission.ROLES_READ: "View the role and permission catalogs.",
    Permission.ROLES_MANAGE: "Create, edit, activate/deactivate, and delete roles.",
    Permission.ORGANIZATIONS_READ: "View organizations.",
    Permission.ORGANIZATIONS_CREATE: "Create organizations.",
    Permission.ORGANIZATIONS_UPDATE: "Edit organizations.",
    Permission.ORGANIZATIONS_DELETE: "Delete (soft-delete) organizations.",
    Permission.ORGANIZATIONS_MANAGE_MEMBERS: "Assign or remove an organization's members.",
    Permission.MODELS_READ: "View the AI model registry.",
    Permission.MODELS_CREATE: "Register new AI models.",
    Permission.MODELS_UPDATE: "Edit AI models and rotate their credentials.",
    Permission.MODELS_DELETE: "Remove AI models from the registry.",
    Permission.EVALUATIONS_READ: "View evaluations and their assigned models.",
    Permission.EVALUATIONS_CREATE: "Create evaluations.",
    Permission.EVALUATIONS_UPDATE: "Edit evaluations and their model assignments.",
    Permission.EVALUATIONS_DELETE: "Delete evaluations.",
    Permission.EVALUATIONS_APPROVE: "Approve or reject evaluations.",
    Permission.EVALUATION_GROUPS_READ: "View evaluation groups.",
    Permission.EVALUATION_GROUPS_MANAGE_MEMBERS: "Manage an evaluation group's members (object-scoped).",
    Permission.EVALUATION_GROUPS_CREATE: "Create evaluation groups.",
    Permission.EVALUATION_GROUPS_UPDATE: "Edit evaluation groups.",
    Permission.EVALUATION_GROUPS_VIEW_METRICS: "View an evaluation group's aggregate metrics (object-scoped).",
    Permission.EVALUATION_GROUPS_VIEW_PERSONAL_METRICS: (
        "View metrics dashboards scoped to your own contributions when a group is set to "
        "`members_personal_metrics` (object-scoped)."
    ),
    Permission.EVALUATION_GROUPS_EXPORT: "Run and download an evaluation group's exports (object-scoped).",
    Permission.EVALUATION_GROUPS_MANAGE: "Full control of any evaluation group (break-glass).",
    Permission.CONVERSATIONS_READ: "View conversations.",
    Permission.CONVERSATIONS_READ_ANY: (
        "View every member's conversations in an evaluation group, not just your own (object-scoped)."
    ),
    Permission.CONVERSATIONS_CREATE: "Start conversations.",
    Permission.CONVERSATIONS_UPDATE: "Edit conversations (regenerate / continue messages).",
    Permission.CONVERSATIONS_DELETE: "Delete conversations.",
    Permission.CONVERSATIONS_PARTICIPATE: "Send messages to a model and stream replies.",
    Permission.FLAGS_READ: "View message flags.",
    Permission.FLAGS_CREATE: "Flag model responses as exploit-worthy.",
    Permission.FLAGS_UPDATE: "Edit message flags.",
    Permission.FLAGS_DELETE: "Delete message flags.",
    Permission.NOTES_READ: "View notes on model responses.",
    Permission.NOTES_CREATE: "Write notes on model responses.",
    Permission.NOTES_UPDATE: "Edit notes.",
    Permission.NOTES_DELETE: "Delete notes.",
    Permission.ANNOTATIONS_READ: "View message annotations and the shared label vocabulary.",
    Permission.ANNOTATIONS_CREATE: "Label messages, from the shared vocabulary or ad hoc.",
    Permission.ANNOTATIONS_DELETE: "Delete own message annotations.",
    Permission.REVIEWS_READ: "View reviews of flagged submissions.",
    Permission.REVIEWS_CREATE: "Assign reviewers to flagged submissions.",
    Permission.REVIEWS_UPDATE: "Record a verdict on an assigned review.",
    Permission.REVIEWS_DELETE: "Unassign a reviewer from a flagged submission.",
    Permission.REVIEWS_ANNOTATE: "Be listed as an assignable reviewer for flagged submissions.",
    Permission.PLATFORM_SETTINGS_READ: "View platform-wide settings (data licensing, registration policy).",
    Permission.PLATFORM_SETTINGS_UPDATE: "Edit platform-wide settings (data licensing, registration policy).",
    Permission.LICENSES_CREATE: "Create data licenses.",
    Permission.LICENSES_UPDATE: "Edit data licenses (own only, unless a manager).",
    Permission.LICENSES_DELETE: "Delete data licenses (own only, unless a manager).",
    Permission.LICENSES_MANAGE: (
        "Edit and delete other users' data licenses, and fill in the legal text of a curated licence "
        "the platform ships without one; a curated licence's other fields stay code-managed."
    ),
    Permission.AUDIT_READ: "View the audit log of sensitive actions and accesses.",
    Permission.SAVED_VIEWS_READ: "View your saved list views.",
    Permission.SAVED_VIEWS_CREATE: "Save a list view (filters, sort, columns).",
    Permission.SAVED_VIEWS_UPDATE: "Rename or update a saved list view.",
    Permission.SAVED_VIEWS_DELETE: "Delete a saved list view.",
    Permission.NOTIFICATIONS_READ: "View your notifications.",
    Permission.NOTIFICATIONS_UPDATE: "Mark your notifications as read or unread.",
}
