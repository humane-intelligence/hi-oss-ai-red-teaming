import { useMemo } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { operations } from '@/lib/api/schema'

export type OrganizationOrderBy = NonNullable<
  operations['list_organizations_endpoint_api_v1_organizations_get']['parameters']['query']
>['order_by']

export function useOrganizations(params: {
  limit: number
  offset: number
  name?: string
  order_by?: OrganizationOrderBy
  enabled?: boolean
  deleted?: boolean
}) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['organizations', params],
    // `deleted=true` is gated on `organizations:delete` server-side; without it the
    // request would 403.
    enabled:
      (params.enabled ?? true) &&
      has('organizations:read') &&
      (!params.deleted || has('organizations:delete')),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/organizations', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              name: params.name || undefined,
              order_by: params.order_by,
              deleted: params.deleted,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// Map organization id -> name for resolving the owning org shown in group lists.
// Gated on organizations:read (held by every role) so it degrades to a short id
// rather than 403-ing for a custom role without it.
export function useOrganizationLookup() {
  const { has } = usePermissions()
  const orgs = useQuery({
    queryKey: ['organization-lookup'],
    enabled: has('organizations:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/organizations', {
          params: { query: { limit: 100, offset: 0 } },
        }),
      ),
  })
  return useMemo(() => {
    const byId = new Map<string, string>()
    for (const o of orgs.data?.items ?? []) byId.set(o.id, o.name)
    return (id: string) => byId.get(id) ?? `${id.slice(0, 8)}…`
  }, [orgs.data])
}

export function useOrganization(
  id: string,
  options?: { enabled?: boolean; suppressErrorToast?: boolean },
) {
  return useQuery({
    queryKey: ['organization', id],
    enabled: id !== '' && (options?.enabled ?? true),
    // Opt-in, not the default: a caller that navigated *to* an organization wants the toast when it
    // 404s. A caller that only decorates another page with its name does not.
    meta: options?.suppressErrorToast ? { suppressErrorToast: true } : undefined,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/organizations/{organization_id}', {
          params: { path: { organization_id: id } },
        }),
      ),
  })
}

export function useOrganizationMembers(organizationId: string) {
  return useQuery({
    queryKey: ['organization-members', organizationId],
    enabled: organizationId !== '',
    // Errors render inline in the members section, and the delete cascade refetches a
    // dying detail page's members into a 404 — a toast would double-report both.
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/organizations/{organization_id}/members', {
          params: { path: { organization_id: organizationId }, query: { limit: 100 } },
        }),
      ),
  })
}
