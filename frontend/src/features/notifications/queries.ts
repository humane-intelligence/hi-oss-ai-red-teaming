import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { NotificationObjectType } from '@/lib/api/types'

type ListParams = {
  limit: number
  offset: number
  read?: boolean
  object_type?: NotificationObjectType
}

export function useNotifications(
  params: ListParams,
  options?: { enabled?: boolean; staleTime?: number },
) {
  return useQuery({
    queryKey: ['notifications', params],
    enabled: options?.enabled ?? true,
    staleTime: options?.staleTime,
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/notifications', { params: { query: params } })),
    placeholderData: keepPreviousData,
  })
}

// Poll the unread count for the header badge. A flat interval (like the backend-status poller) is
// enough — the count isn't time-critical — and a transient blip must not pop a global error toast.
const UNREAD_POLL_MS = 30_000

export function useUnreadCount() {
  return useQuery({
    queryKey: ['notifications-unread'],
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/notifications', {
          params: { query: { limit: 1, offset: 0, read: false } },
        }),
      ).total,
    refetchInterval: UNREAD_POLL_MS,
  })
}
