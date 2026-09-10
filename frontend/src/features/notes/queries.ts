import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'

// The server maximum, and it sorts newest-first — so a conversation past this ceiling
// shows the most recent and says so.
export const NOTES_PAGE_SIZE = 100

// The caller's own notes on one conversation — reads are author-scoped server-side, so a
// fellow reviewer's notes never come back; a holder of the global `evaluation_groups:manage`
// (admin) is the exception and reads every author's, which is what `authorLabel` renders.
// Gated on notes:read because `owner` holds the whole reviews:* set (and so reaches the
// review view) but no note key at all; ungated it would eat a 403 on every review it opens.
export function useConversationNotes(conversationId: string) {
  const { has } = usePermissions()
  const params = { conversation_id: conversationId, limit: NOTES_PAGE_SIZE, offset: 0 }
  return useQuery({
    queryKey: ['notes', params],
    enabled: conversationId !== '' && has('notes:read'),
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/notes', { params: { query: params } })),
    // The transcript card renders the failure inline; a toast would say it twice.
    meta: { suppressErrorToast: true },
  })
}

// The caller's recently-deleted notes on one conversation, newest tombstone first — the
// browsable half of restore (the delete toast's Undo is the other). Gated on
// notes:delete: only a caller who could delete can restore, and the server scopes
// the listing to their own deletes regardless.
export function useDeletedConversationNotes(conversationId: string, enabled: boolean) {
  const { has } = usePermissions()
  const params = {
    conversation_id: conversationId,
    deleted: true,
    order_by: '-deleted_at' as const,
    limit: NOTES_PAGE_SIZE,
    offset: 0,
  }
  return useQuery({
    queryKey: ['notes', params],
    enabled: enabled && conversationId !== '' && has('notes:delete'),
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/notes', { params: { query: params } })),
    meta: { suppressErrorToast: true },
  })
}
