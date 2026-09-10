import { useMemo } from 'react'
import { useAuth } from './auth-context'

export type PermissionChecker = {
  has: (p: string) => boolean
  hasAny: (ps: string[]) => boolean
  hasAll: (ps: string[]) => boolean
}

// Build a capability checker over an explicit permission set. Used for the
// caller's global permissions (below) and for an object's per-caller
// `user_permissions` (e.g. an evaluation group via `useObjectPermissions`), so
// object actions gate on object authority — the same set the server authorizes
// on — rather than the global union.
export function permissionChecker(permissions: Iterable<string>): PermissionChecker {
  const permSet = new Set(permissions)
  return {
    has: (p) => permSet.has(p),
    hasAny: (ps) => ps.some((p) => permSet.has(p)),
    hasAll: (ps) => ps.every((p) => permSet.has(p)),
  }
}

// Capability gating for the UI. Source: effective permissions from /auth/me
// (same strings as the JWT claim). Server enforces on every request — this is
// presentation-only. These are the caller's *global* permissions; per-object
// authority comes from the object's `user_permissions` (`useObjectPermissions`).
export function usePermissions() {
  const { user } = useAuth()
  return useMemo(() => {
    const permissions = user?.permissions ?? []
    const roles = user?.roles ?? []
    const roleSet = new Set(roles.map((r) => r.name))
    return {
      permissions,
      roles,
      ...permissionChecker(permissions),
      hasRole: (name: string) => roleSet.has(name),
      hasResource: (resource: string) => permissions.some((p) => p.startsWith(`${resource}:`)),
    }
  }, [user])
}

// A checker over an object's per-caller `user_permissions` (e.g. the effective
// in-group authority on an evaluation group). Gate object-scoped actions on
// this, mirroring the server's per-object authorization.
export function useObjectPermissions(
  permissions: readonly string[] | undefined,
): PermissionChecker {
  return useMemo(() => permissionChecker(permissions ?? []), [permissions])
}

// A checker over the caller's global permissions *union* an object's
// `user_permissions`. Mirrors the gates that accept either source — the JWT carries
// global roles only, so a caller whose authority comes from an in-group role holds
// nothing globally. Use it wherever the server takes global-or-object; use
// `useObjectPermissions` where authority must be object-scoped.
//
// `user_permissions` is a whole-role set, not a filtered one: an in-group `owner` "holds"
// `users:invite`, `saved_views:*` and everything else that role grants. Only ask this for keys
// the object actually governs — a platform-wide permission read off a group means nothing.
export function useEffectivePermissions(
  objectPermissions: readonly string[] | undefined,
): PermissionChecker {
  const { permissions } = usePermissions()
  return useMemo(
    () => permissionChecker([...permissions, ...(objectPermissions ?? [])]),
    [permissions, objectPermissions],
  )
}
