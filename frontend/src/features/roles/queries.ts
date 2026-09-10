import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'

// The permission catalog is small (~56); the max page (100) covers it in one fetch.
const CATALOG_LIMIT = 100

export function useRolesAdmin(params: {
  limit: number
  offset: number
  includeInactive: boolean
  deleted?: boolean
}) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['roles', params],
    // `deleted=true` is gated on `roles:manage` server-side; without it the request would 403.
    enabled: has('roles:read') && (!params.deleted || has('roles:manage')),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/roles', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              // The tombstone view asks for everything: `include_inactive` narrows both
              // branches alike, so leaving it off would hide a deactivated role's
              // tombstone from the only surface that can restore it.
              include_inactive: params.deleted || params.includeInactive,
              deleted: params.deleted,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// The form routes gate on roles:manage, but these reads gate on roles:read — safe because
// roles:manage is non-delegable and its only holder (admin) always also holds roles:read.
export function useRole(id: string) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['role', id],
    enabled: id !== '' && has('roles:read'),
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/roles/{role_id}', { params: { path: { role_id: id } } })),
  })
}

export function useAllPermissions() {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['permissions'],
    enabled: has('roles:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/permissions', { params: { query: { limit: CATALOG_LIMIT } } }),
      ),
  })
}
