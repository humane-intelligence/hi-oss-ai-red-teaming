import { useMemo } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { operations } from '@/lib/api/schema'
import type { EvaluationGroupAccessLevel, PublicationStatus } from '@/lib/api/types'

export type EvaluationGroupOrderBy = NonNullable<
  operations['list_evaluation_groups_endpoint_api_v1_evaluation_groups_get']['parameters']['query']
>['order_by']

export function useEvaluationGroups(
  params: {
    limit: number
    offset: number
    search?: string
    status?: PublicationStatus
    order_by?: EvaluationGroupOrderBy
    access_level?: EvaluationGroupAccessLevel
    // Server-side lifecycle filter: only groups that accept new evaluations
    // (`approved`/`published`) — the evaluation-create picker passes `true` so the
    // allowlist stays backend-owned and pagination applies to the relevant rows.
    accepts_evaluations?: boolean
    // Managers (`evaluation_groups:manage`) pass this to lift the visibility scope
    // and list every group — incl. others' private/draft ones. Sending it without
    // the permission is a 403, so callers must gate it on the permission.
    all_groups?: boolean
  },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['evaluation-groups', params],
    enabled: options?.enabled ?? true,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluation-groups', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              search: params.search || undefined,
              status: params.status || undefined,
              order_by: params.order_by,
              access_level: params.access_level,
              accepts_evaluations: params.accepts_evaluations,
              all_groups: params.all_groups || undefined,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// The endpoint's maximum page size — a larger `limit` is a 422.
const LOOKUP_PAGE_SIZE = 100

// Bounds the walk. 2000 groups is far past anything this console has seen, and an unbounded loop
// against a growing `total` would keep issuing requests for as long as rows are being added.
const LOOKUP_MAX_PAGES = 20

// Map group id -> title for the parent group shown in flat evaluation lists.
// Gated on evaluation_groups:read, so a persona without it gets an empty map
// rather than a 403.
export function useGroupLookup() {
  const { has } = usePermissions()
  // `/evaluations` lifts its own scope for this permission (a manager sees evaluations in groups
  // they are not a member of), while this endpoint lifts it only behind `all_groups` — without
  // this the manager's list carries rows whose group the lookup never sees.
  const allGroups = has('evaluation_groups:manage') || undefined
  const groups = useQuery({
    // `all_groups` is part of the key: it changes the result set, and two personas must not share
    // one cache entry. Nested under `evaluation-groups` so `useInvalidateGroup` reaches it by
    // prefix — at this `staleTime` a lookup nothing invalidates keeps naming a group the reader
    // just created or renamed.
    queryKey: ['evaluation-groups', 'lookup', allGroups ?? false],
    enabled: has('evaluation_groups:read'),
    // Names change rarely and this can cost several round trips, so it outlives the global 30s.
    staleTime: 5 * 60_000,
    // Pages to the end rather than reading the first page: one page is the server's maximum, so a
    // single read silently fails to name every group past the hundredth. `total` says when to stop,
    // and an empty page breaks the loop in case it ever disagrees with `items`.
    queryFn: async () => {
      const names: [string, string][] = []
      let total = Number.POSITIVE_INFINITY
      for (let page = 0; page < LOOKUP_MAX_PAGES && names.length < total; page++) {
        const response = unwrap(
          await apiClient.GET('/api/v1/evaluation-groups', {
            params: {
              query: { limit: LOOKUP_PAGE_SIZE, offset: names.length, all_groups: allGroups },
            },
          }),
        )
        total = response.total
        for (const g of response.items) names.push([g.id, g.title ?? 'Untitled draft'])
        if (response.items.length === 0) break
      }
      return names
    },
  })
  const resolve = useMemo(() => {
    const byId = new Map(groups.data ?? [])
    // `null`, never a label: the caller is the only one that knows whether an unnamed group may
    // still be linked. A failed read empties the map, so a label here would answer every row
    // confidently with a name nobody looked up.
    return (id: string) => byId.get(id) ?? null
  }, [groups.data])
  // Pending is not the same answer as unresolved — the caller renders a placeholder rather than
  // naming a group it has not looked up yet. Hand-rolled rather than `isLoading`, which goes false
  // while `fetchStatus === 'paused'` (offline) and would flip the cells to "no name" mid-flight.
  return { resolve, isPending: groups.isPending && groups.fetchStatus !== 'idle' }
}

export function useEvaluationGroup(id: string) {
  return useQuery({
    queryKey: ['evaluation-group', id],
    enabled: id !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluation-groups/{group_id}', {
          params: { path: { group_id: id } },
        }),
      ),
  })
}

export function useGroupMembers(
  groupId: string,
  opts: {
    search?: string
    roleId?: string
    limit?: number
    enabled?: boolean
    keepPrevious?: boolean
  } = {},
) {
  const limit = opts.limit ?? 100
  return useQuery({
    queryKey: ['evaluation-group-members', groupId, opts.search ?? '', opts.roleId ?? '', limit],
    enabled: groupId !== '' && (opts.enabled ?? true),
    // Opt-in for the debounced pickers (each keystroke is a new key) so the listbox doesn't blank per
    // char. Off by default: the group-detail views key only on groupId, on an unkeyed route, so keeping
    // the prior page there would flash the previous group's members on a group→group switch.
    placeholderData: opts.keepPrevious ? keepPreviousData : undefined,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluation-groups/{group_id}/members', {
          params: {
            path: { group_id: groupId },
            query: { limit, search: opts.search || undefined, role_id: opts.roleId || undefined },
          },
        }),
      ),
  })
}

// Whole-event aggregate metrics — gated server-side by the group's configured
// metrics-access level. The group detail injects `evaluation_groups:view_metrics` into
// `user_permissions` whenever the caller would be admitted (full or personal scope), so callers
// pass `enabled` from that key to avoid a 403 (and its error toast). The response's own `scope`
// field says whether the numbers are the full aggregate or the caller's personal slice.
export function useEvaluationGroupMetrics(groupId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['evaluation-group-metrics', groupId],
    enabled: groupId !== '' && (options?.enabled ?? true),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluation-groups/{group_id}/metrics', {
          params: { path: { group_id: groupId } },
        }),
      ),
  })
}

// The annotator list is gated server-side on `evaluation_groups:manage_members`
// (owner / break-glass), unlike the read-visible member list — so callers pass
// `enabled` from their per-group authority to avoid a 403 (and its error toast)
// for members who can't manage.
export function useGroupAnnotators(
  groupId: string,
  options?: { enabled?: boolean; search?: string },
) {
  return useQuery({
    queryKey: ['evaluation-group-annotators', groupId, options?.search ?? ''],
    enabled: groupId !== '' && (options?.enabled ?? true),
    placeholderData: keepPreviousData,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluation-groups/{group_id}/annotators', {
          params: {
            path: { group_id: groupId },
            query: { limit: 100, search: options?.search || undefined },
          },
        }),
      ),
  })
}
