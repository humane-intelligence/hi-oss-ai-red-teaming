import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { SavedViewResource } from '@/lib/api/types'

// The caller's saved views for one list, ordered by name (server default).
// Personal data — the endpoint is owner-scoped, so no user sees another's views.
export function useSavedViews(resource: SavedViewResource) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['saved-views', resource],
    enabled: has('saved_views:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/saved-views', {
          params: { query: { resource, limit: 100 } },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}
