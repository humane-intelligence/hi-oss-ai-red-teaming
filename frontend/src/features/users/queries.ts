import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { operations } from '@/lib/api/schema'
import type { UserStatus } from '@/lib/api/types'

export type UserOrderBy = NonNullable<
  operations['list_users_endpoint_api_v1_auth_users_get']['parameters']['query']
>['order_by']

// `objectAssignable` only ever narrows: `is_object_assignable=false` would ask the API
// for the *complement*, which no caller wants, so the option can't express it.
export function useRoles(options?: { enabled?: boolean; objectAssignable?: true }) {
  const objectAssignable = options?.objectAssignable
  return useQuery({
    // Keyed on the filter: the in-group subset and the full catalog are different
    // responses. Still prefix-matched by the `['roles']` invalidations on mutation.
    queryKey: ['roles', objectAssignable ?? null],
    // GET /roles is gated on roles:read (global admin/owner). Callers whose authority is object-scoped
    // must gate this on has('roles:read') to avoid a 403 + error toast.
    enabled: options?.enabled ?? true,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/roles', {
          params: {
            query: {
              limit: 100,
              ...(objectAssignable !== undefined && { is_object_assignable: objectAssignable }),
            },
          },
        }),
      ),
  })
}

export function useUsers(
  params: {
    limit: number
    offset: number
    status?: UserStatus
    email?: string
    role_id?: string
    order_by?: UserOrderBy
    deleted?: boolean
  },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['users', params],
    enabled: options?.enabled ?? true,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/auth/users', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              ...(params.status !== undefined && { status: params.status }),
              ...(params.email !== undefined && { email: params.email }),
              ...(params.role_id !== undefined && { role_id: params.role_id }),
              order_by: params.order_by,
              deleted: params.deleted,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

export function useUser(id: string) {
  return useQuery({
    queryKey: ['user', id],
    enabled: id !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/auth/users/{user_id}', {
          params: { path: { user_id: id } },
        }),
      ),
  })
}

// Map user id -> email for resolving ids shown elsewhere (reviewers, authors).
// Gated on users:read so personas without it (e.g. red-teamers viewing an
// evaluation) fall back to a short id instead of triggering a 403 toast.
export function useUserLookup() {
  const { has } = usePermissions()
  const users = useUsers({ limit: 100, offset: 0 }, { enabled: has('users:read') })
  const byId = new Map<string, string>()
  for (const u of users.data?.items ?? []) byId.set(u.id, u.email)
  // Non-admins can't read the users list, so ids stay unresolved — show a neutral label
  // rather than leaking a raw UUID fragment (BE owns proper name resolution).
  return (id: string) => byId.get(id) ?? 'unknown user'
}
