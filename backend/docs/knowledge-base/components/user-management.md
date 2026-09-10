---
tags: [component, auth, authorization]
aliases: [Users, User CRUD, User management]
---

# User management

Admin-facing user lifecycle — list / read / update / soft-delete users, switch an account's status, mail a password reset, resend or revoke a pending invitation — plus the **role-assignment authority** rule that stops a manager from granting or stripping privileges it doesn't itself hold. There is **no create-user endpoint**: accounts are provisioned through platform invitations or self-registration ([Authentication (auth)](authentication.md)), so admins don't mint credentialed accounts directly (the `users:create` permission still exists in the vocabulary but backs no route today). Routes: `app/api/v1/auth/users.py` (router prefix `/users`, mounted under `/api/v1/auth`); services: `app/core/auth/services/users.py`.

Account onboarding (registration, invitations, login, password reset) is [Authentication (auth)](authentication.md); the role/permission vocabulary is [RBAC - global roles](rbac-global-roles.md).

## Endpoints

| Method + path | What | Permission |
|---|---|---|
| `GET /api/v1/auth/users` | Paginated list (`Page[UserResponse]`) with filters + `order_by`; `?deleted=true` lists tombstones inside the restore window | `users:read` (+ `users:delete` for `deleted=true`) |
| `GET /api/v1/auth/users/{id}` | Fetch one | `users:read` |
| `PATCH /api/v1/auth/users/{id}` | Partial update; a supplied `role_ids` replaces the whole role set | `users:update` (+ `users:manage_admin` to add/remove admin or owner) |
| `DELETE /api/v1/auth/users/{id}` | Soft-delete (`deleted_at`); **409** if the user is the sole `owner` of an object | `users:delete` (+ guards below) |
| `POST /api/v1/auth/users/{id}/restore` | Clear the tombstone; roles / status / org come back untouched | `users:delete` (+ `users:manage_admin` for an elevated account) |
| `POST /api/v1/auth/users/{id}/force-logout` | Revoke all of that user's active sessions; `204` | `users:manage_sessions` |
| `POST /api/v1/auth/users/force-logout` | The same, in bulk — `BulkRequest[ForceLogoutTarget]` → `BulkResponse[ForceLogoutTarget]` | `users:manage_sessions` |
| `POST /api/v1/auth/users/{id}/status` | Activate / deactivate an account | `users:update` |
| `POST /api/v1/auth/users/status` | The same, in bulk — `BulkRequest[UserStatusTarget]` | `users:update` |
| `POST /api/v1/auth/users/{id}/password-reset` | Mail a fresh reset link; `204` | `users:update` |
| `POST /api/v1/auth/users/password-reset` | The same, in bulk — `PasswordResetBulkRequest` | `users:update` |
| `POST /api/v1/auth/users/{id}/invitation/resend` | Re-issue + re-mail the platform invitation of an `invited` account | `users:invite` |
| `DELETE /api/v1/auth/users/{id}/invitation` | Revoke the pending **platform** accept link; `204` | `users:invite` |

`order_by` is a `UserOrderBy` literal (a column name; `-` prefix = descending). `UserFilters`: `status`, case-insensitive substring on `email` / `first_name` / `last_name` (escaped `LIKE`), `role_id`, and `search` — one substring matched against `email` OR `first_name` OR `last_name`, for a picker's single search box. Delete returns `204`.

`UserResponse` additionally embeds `invitation` (a `UserInvitationInfo`: effective `status` + `expires_at`) for platform-scoped invitations only, so a user list can render "invited / expired / revoked" without a second call. A `pending` row past its expiry projects as `expired` without a write. See [Invitation](../data-models/invitation.md).

## Role-assignment authority

Holding `users:create|update|delete` is **not** enough to hand out an **elevated** role. `ELEVATED_ROLE_PERMISSIONS` (`app/core/auth/roles.py`) maps an elevated role to the extra permission its management needs — both `admin` **and** `owner` map to `users:manage_admin`.

- `assert_can_assign_roles(caller, requested, held)` — the caller may add **or revoke** an elevated role only if it holds that role's elevated permission. Checked in both directions, so a manager without `users:manage_admin` can neither promote someone to `admin` nor silently demote an existing admin. `resolve_assignable_roles` wraps it and validates every `role_id` resolves to a live, **active** role (unknown or inactive id → 400).
- `assert_can_delete_user(caller, user)` — you can't delete a user holding an elevated role you can't manage (a delete is an implicit demotion). The route also blocks **deleting your own account** (403).
- `assert_can_manage_status(caller, user)` / `assert_can_manage_invitation(caller, user)` — the same `_blocked_elevated_roles` check applied to the two new admin actions: you can't deactivate an elevated account, nor kill or re-issue its invitation, without that role's elevated permission.
- `objects_solely_held_by(db, user_id)` — a delete is additionally rejected with **409** when the user is the sole live holder of an object's protected role (today: the last `owner` of an evaluation group). Reassign ownership before deleting them — the same last-owner invariant the member-management routes enforce. See [Object roles - per-object permissions](object-roles-per-object-permissions.md).

So `users:manage_admin` is the break-glass for the `admin` and `owner` roles: an `owner`/manager can run the full user CRUD but never mint, strip, deactivate, or delete one without it.

## Account status

Only **`active`** and **`inactive`** are settable (`SettableUserStatus`). An account still onboarding (`invited` / `pending`) is a **409**: forcing it `active` would leave a passwordless account that still cannot log in, and overwriting either status would discard onboarding state irrecoverably.

- Deactivating also **revokes the user's sessions**, so access stops immediately rather than when the current token expires.
- Re-sending a status the user already holds succeeds and changes nothing.
- Targeting **your own account** is a 403 — an admin deactivating themselves locks themselves out of the console.
- Audited as `user.status_change`, with a `changed_fields`-reduced before/after.

The bulk variant carries the target status **per row** (there is no batch-level field), so a UI activating a selection sends the same value on every row. Like force-logout, the revoke happens *before* the audit row and `dry_run=true` touches neither Redis nor the trail.

## Admin-triggered password reset

Mails a fresh reset link and revokes any pending token. The link goes to the **user's own mailbox** — the caller never sees the token and cannot set the password themselves, which is why this route carries **no elevation gate** (unlike delete / deactivate / invitation-revoke). `assert_password_reset_allowed` rejects an account that could not act on the link: **409** for one that is not `active`, or has no password to reset (it signs in through an identity provider). Unlike the unauthenticated self-service request — always 204, to avoid enumerating accounts — this one reports why it could not send. Audited as `user.credential_reset`.

The mail is the `password_reset` template with `triggered_by_admin=true`, which switches the copy: it must not tell a recipient they requested a reset they never asked for. See [Email](email.md).

The bulk variant rejects the **same user in more than one row** (422, like a duplicate `row_key`): issuing a token revokes the account's previous one, so the extra rows would only mail dead links. Mails are dispatched *after* `apply_bulk` commits (a Celery task enqueued inside a savepoint that later rolls back would leave the broker holding work for a token that no longer exists), committing per row so the task's 1-second countdown isn't outrun. A row can therefore report `ok` even if its message was never queued — the caller gets **one** in-app notification for the whole batch when that happens.

## Resend / revoke an invitation

Both act on the **platform** scope only; a group-scoped token the same account holds is untouched.

- **Resend** — 409 unless the account is `invited` (an active, self-registering or deactivated user has no invitation to resend). Roles are **not** re-granted: this re-sends the standing invitation, so it never re-checks role grantability. It always issues a *platform* invitation, so for an account invited only into a group it adds one rather than re-sending the group invite. Audited as `invitation.resend`.
- **Revoke** — kills the live accept link; the invited `User` row **stays** (it may already hold roles handed out at invite time, and a re-invite reuses it). 404 when there is no pending platform invitation. Audited as `invitation.revoke`.

Both take the **user row `FOR UPDATE`** first. That lock is what serialises them against each other: `invitations` carries no uniqueness over (user, pending), so two unlocked callers would each see the other's row vanish under the re-check and revoke would end up reporting 404 over a live token. See the lock-order rule in [Authentication (auth)](authentication.md).

## Restore a deleted account

`?deleted=true` on the list serves the restorable set — every actor's deletes, since there is no owner tier under `users:delete` — and `POST /auth/users/{id}/restore` revives one, inside `RESTORE_WINDOW_DAYS`. The elevation guard from the delete applies again (`assert_can_restore_user`), so bringing back an admin or owner needs `users:manage_admin`.

Two things the restore does **not** do: it does not un-tombstone the account's `provider_identities` (the next external login re-links them), and it does not revoke anything — a token minted before the delete and still unexpired works again. Mechanics: [Restore - reading tombstones back](restore-soft-deleted-items.md).

## Force-logout

Kicking a user out of every session is its **own** admin-only permission, `users:manage_sessions` — not part of `users:update`. Both routes write the same Redis revocation marker (`revoke_user_sessions`, mechanics in [Authentication (auth)](authentication.md)) and audit as `user.force_logout`.

The bulk route rides the platform envelope: up to `BULK_MAX_ROWS` rows, one `{user_id}` each, always `200` when the envelope is well-formed (an unknown user id lands in `results[].error` as a per-row 404), and `dry_run=true` walks the whole path while revoking nothing. It is deliberately **not** `@transactional` — `apply_bulk` owns the transaction boundary. See [Pagination, bulk and soft-delete](pagination-bulk-and-soft-delete.md).

Two ordering decisions worth knowing, both documented in the handler:

- The **revoke happens before the audit row**, so if the batch's commit later fails the user is still logged out (safe) but without a trail — the acceptable direction.
- A **Redis error mid-batch propagates** (a non-`APIError` aborts the whole bulk with a 500): rows revoked so far stay revoked, their audit rows roll back. Same safe direction, wider blast radius — the caller retries.

## Related

- [Authentication (auth)](authentication.md) — the revocation marker behind force-logout
- [RBAC - global roles](rbac-global-roles.md)
- [User and Role](../data-models/user-and-role.md)
- [Invitation](../data-models/invitation.md)
- [Pagination, bulk and soft-delete](pagination-bulk-and-soft-delete.md) — the bulk envelope
- [Email](email.md) — the reset / invitation mails and their delivery-failure notices
- [Notifications (in-app feed)](notifications.md) — where a lost bulk mail is reported
- [Audit log](audit-log.md) — `user.force_logout`, `user.status_change`, `user.credential_reset`, `invitation.resend/revoke`
- [API - overview and conventions](api-overview-and-conventions.md)
