import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { MarkNotificationsRequest } from '@/lib/api/types'

// Mark the caller's notifications read/unread. An empty `ids` marks all. Invalidates both the
// list and the header unread-count so the badge and any open list refresh together.
export function useMarkNotifications() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: MarkNotificationsRequest) =>
      unwrap(await apiClient.POST('/api/v1/notifications/mark', { body })),
    onSuccess: (result, body) => {
      qc.invalidateQueries({ queryKey: ['notifications'] })
      qc.invalidateQueries({ queryKey: ['notifications-unread'] })
      // Confirm the bulk "mark all" (empty/omitted ids) when it changed something; per-row toasts
      // would be noise, and a no-op mark-all (nothing to flip) needs no confirmation.
      if ((body.ids ?? []).length === 0 && result.updated > 0) {
        const noun = result.updated === 1 ? 'notification' : 'notifications'
        toast.success(`Marked ${result.updated} ${noun} ${body.read ? 'read' : 'unread'}`)
      }
    },
  })
}
