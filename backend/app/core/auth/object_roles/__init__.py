"""Generic per-object role assignment — the django-guardian analogue for this platform.

Assign roles to a user *on a specific object* (today an `EvaluationGroup`; the
layer is object-type agnostic). A second authorization scope alongside the
platform-wide permissions carried in the JWT: object-scope access is resolved
per request from `ObjectRoleAssignment` rows because the JWT is frozen at mint
time.

**The override — the single most surprising rule here:** once a user holds any
role on an object, those roles' permissions are their *entire* effective
permission set on it, never combined with their global JWT permissions. A
non-member has no object authority at all (a global permission grants no
object-scope say), and only the break-glass `super_permission` survives the
override. Object authority comes solely from these assignments — there is no
creator/ownership fallback; a creator controls their object because the owning
domain grants them a role on create.

Pieces:

- `registry` — `ObjectType`, `ObjectScopeSpec`, and `OBJECT_ROLE_REGISTRY`: the
  roles assignable per object type and the break-glass `super_permission`. What a
  held role *grants* in-object is its own `Role.permissions`, not a copy here.
- `models` — the polymorphic `ObjectRoleAssignment` table.
- `service` — the assignment store (`add_member` / `set_member_roles` /
  `remove_member` / `list_members`) plus `resolve_object_access`, the gate input.
  Object roles are the only source of object authority — a member gets their held
  roles' permissions, a non-member gets none (global JWT permissions never grant
  object-scope authority) — plus the break-glass `super_permission` short-circuit.
- `schemas` — reusable request/response shapes for the member endpoints.

Object *visibility / ownership* (e.g. a group's `public` access level) stays with
the owning domain; this layer never loads the target object.
"""
