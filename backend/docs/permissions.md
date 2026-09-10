# Roles & permissions matrix

Auto-generated from `app/core/auth/roles.py` and the object-role registry
(`app/core/auth/object_roles/registry.py`) by `make permissionsdump` — **do not
edit by hand**. Regenerate after any change to the canonical roles, permissions,
or object-scope policy and commit the result in the same diff (the pre-push hook
does this for you; CI fails on drift).

## Roles

| Role | Display name | Description |
| --- | --- | --- |
| `admin` | Admin | Full administrative access: manage users and invite any role. |
| `owner` | Owner | Manages a single instance and invites owners, red teamers, annotators, and viewers. |
| `red_teamer` | Red Teamer | Participates as a red teamer in assigned evaluations. |
| `annotator` | Annotator | Annotates conversations and evaluation outputs. |
| `viewer` | Viewer | Views aggregated evaluation results and dashboards. |

## Permission matrix

Columns are roles. ✓ = granted globally (carried in the JWT); ○ = conferred only at object scope via a break-glass permission (see [Object scopes](#object-scopes)). Permissions are grouped by the resource they gate.

| Permission | `admin` | `owner` | `red_teamer` | `annotator` | `viewer` |
| --- | :-: | :-: | :-: | :-: | :-: |
| **users** | | | | | |
| `users:read` | ✓ |  |  |  |  |
| `users:update` | ✓ |  |  |  |  |
| `users:delete` | ✓ |  |  |  |  |
| `users:invite` | ✓ | ✓ |  |  |  |
| `users:manage_admin` | ✓ |  |  |  |  |
| `users:manage_sessions` | ✓ |  |  |  |  |
| **roles** | | | | | |
| `roles:read` | ✓ | ✓ |  |  |  |
| `roles:manage` | ✓ |  |  |  |  |
| **organizations** | | | | | |
| `organizations:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `organizations:create` | ✓ |  |  |  |  |
| `organizations:update` | ✓ |  |  |  |  |
| `organizations:delete` | ✓ |  |  |  |  |
| `organizations:manage_members` | ✓ |  |  |  |  |
| **models** | | | | | |
| `models:read` | ✓ | ✓ |  |  |  |
| `models:create` | ✓ |  |  |  |  |
| `models:update` | ✓ |  |  |  |  |
| `models:delete` | ✓ |  |  |  |  |
| **evaluations** | | | | | |
| `evaluations:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `evaluations:create` | ✓ | ✓ |  |  |  |
| `evaluations:update` | ✓ | ✓ |  |  |  |
| `evaluations:delete` | ✓ | ✓ |  |  |  |
| `evaluations:approve` | ✓ |  |  |  |  |
| **evaluation_groups** | | | | | |
| `evaluation_groups:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `evaluation_groups:manage_members` | ○ | ✓ |  |  |  |
| `evaluation_groups:create` | ✓ | ✓ |  |  |  |
| `evaluation_groups:update` | ✓ | ✓ |  |  |  |
| `evaluation_groups:view_metrics` | ✓ | ✓ |  |  |  |
| `evaluation_groups:view_personal_metrics` | ○ |  | ✓ |  |  |
| `evaluation_groups:export` | ○ | ✓ |  |  |  |
| `evaluation_groups:manage` | ✓ |  |  |  |  |
| **conversations** | | | | | |
| `conversations:read` | ✓ | ✓ | ✓ |  |  |
| `conversations:create` | ✓ |  | ✓ |  |  |
| `conversations:update` | ✓ |  | ✓ |  |  |
| `conversations:delete` | ✓ |  | ✓ |  |  |
| `conversations:read_any` | ○ | ✓ |  |  |  |
| `conversations:participate` | ✓ | ✓ | ✓ |  |  |
| **flags** | | | | | |
| `flags:read` | ✓ |  | ✓ | ✓ |  |
| `flags:create` | ✓ |  | ✓ |  |  |
| `flags:update` | ✓ |  | ✓ |  |  |
| `flags:delete` | ✓ |  | ✓ |  |  |
| **notes** | | | | | |
| `notes:read` | ✓ |  | ✓ | ✓ |  |
| `notes:create` | ✓ |  | ✓ | ✓ |  |
| `notes:update` | ✓ |  | ✓ | ✓ |  |
| `notes:delete` | ✓ |  | ✓ | ✓ |  |
| **annotations** | | | | | |
| `annotations:read` | ✓ | ✓ |  | ✓ |  |
| `annotations:create` | ✓ |  |  | ✓ |  |
| `annotations:delete` | ✓ |  |  | ✓ |  |
| **reviews** | | | | | |
| `reviews:read` | ✓ | ✓ | ✓ | ✓ |  |
| `reviews:create` | ✓ | ✓ |  | ✓ |  |
| `reviews:update` | ✓ | ✓ |  | ✓ |  |
| `reviews:delete` | ✓ | ✓ |  | ✓ |  |
| `reviews:annotate` | ○ |  |  | ✓ |  |
| **platform_settings** | | | | | |
| `platform_settings:read` | ✓ |  |  |  |  |
| `platform_settings:update` | ✓ |  |  |  |  |
| **licenses** | | | | | |
| `licenses:create` | ✓ | ✓ |  |  |  |
| `licenses:update` | ✓ | ✓ |  |  |  |
| `licenses:delete` | ✓ | ✓ |  |  |  |
| `licenses:manage` | ✓ |  |  |  |  |
| **audit** | | | | | |
| `audit:read` | ✓ |  |  |  |  |
| **saved_views** | | | | | |
| `saved_views:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `saved_views:create` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `saved_views:update` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `saved_views:delete` | ✓ | ✓ | ✓ | ✓ | ✓ |
| **notifications** | | | | | |
| `notifications:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `notifications:update` | ✓ | ✓ | ✓ | ✓ | ✓ |

## Object scopes

Some objects carry a *second*, per-object authorization scope (the `object_roles` layer). Roles assigned on a specific object are resolved per request — **not** carried in the JWT — and a held object role grants its own row from the matrix above, scoped to that object. The override rule: once a user holds any role on an object, those permissions are their *entire* effective authority on it (a global JWT permission grants no object-scope say); only the break-glass permission below survives it, conferring full control of every object of that type. A *protected role*, when set, must always retain at least one live holder — its last holder can be neither removed nor demoted (even by a break-glass admin), so the object can't be orphaned of that authority.

| Object type | Assignable roles | Break-glass permission | Protected role |
| --- | --- | --- | --- |
| `evaluation_group` | `owner`, `red_teamer`, `annotator`, `viewer` | `evaluation_groups:manage` | `owner` |

## Role elevations

Assigning *or* revoking these roles requires the listed permission, held in addition to the operation's own gate (see `services.users.assert_can_assign_roles`).

| Role | Required permission |
| --- | --- |
| `admin` | `users:manage_admin` |
| `owner` | `users:manage_admin` |
