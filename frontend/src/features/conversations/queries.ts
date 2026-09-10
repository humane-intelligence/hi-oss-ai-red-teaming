import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { useEffectivePermissions, usePermissions } from '@/lib/auth/use-permissions'

// Every read below is gated on `conversations:read` server-side. The gate is repeated in
// `enabled` rather than left to the route guards: ConversationsSection renders inside the
// evaluation detail page, which only needs `evaluations:read`, so an unguarded query fires a
// request that can only 403 and surfaces as an error panel.
const READ = 'conversations:read'

// The server accepts the permission from the JWT *or* from a role on the parent group, but only
// the caller knows the object — so callers pass the group's `user_permissions` and the union is
// applied here. Deliberately not an `enabled` override: that would let a caller replace the
// permission check with an unrelated condition (a collapsed panel, a lazy tab).
type ObjectAuthority = { groupPermissions?: readonly string[] }
export function useConversationGroups(evaluationId: string, options: ObjectAuthority = {}) {
  const { has } = useEffectivePermissions(options.groupPermissions)
  return useQuery({
    queryKey: ['conversation-groups', evaluationId],
    enabled: evaluationId !== '' && has(READ),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/conversation-groups', {
          params: { path: { evaluation_id: evaluationId }, query: { limit: 100, offset: 0 } },
        }),
      ),
  })
}

// The caller's recently-deleted conversations in one evaluation, newest tombstone first.
// Gated on conversations:delete: the deleted listing is the restore surface, and only a
// caller who could delete can restore (the server scopes it to their own deletes anyway).
export function useDeletedConversations(evaluationId: string, enabled: boolean) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['conversations', 'deleted', evaluationId],
    enabled: enabled && evaluationId !== '' && has('conversations:delete'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/conversations', {
          params: {
            path: { evaluation_id: evaluationId },
            query: { deleted: true, order_by: '-deleted_at', limit: 100, offset: 0 },
          },
        }),
      ),
    // The disclosure renders the failure inline, like the notes twin.
    meta: { suppressErrorToast: true },
  })
}

// Flat list of the caller's conversation groups across all visible evaluations. Names no object,
// so — like the route it calls — it stays on the global permission.

export function useAllConversationGroups(params: { limit: number; offset: number }) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['conversation-groups', 'all', params],
    enabled: has(READ),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/conversation-groups', {
          params: { query: { limit: params.limit, offset: params.offset } },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

export function useConversationGroup(
  evaluationId: string,
  groupId: string,
  options: ObjectAuthority = {},
) {
  const { has } = useEffectivePermissions(options.groupPermissions)
  return useQuery({
    queryKey: ['conversation-group', groupId],
    enabled: evaluationId !== '' && groupId !== '' && has(READ),
    queryFn: async () =>
      unwrap(
        await apiClient.GET(
          '/api/v1/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}',
          { params: { path: { evaluation_id: evaluationId, conversation_group_id: groupId } } },
        ),
      ),
  })
}

export function useConversation(
  evaluationId: string,
  conversationId: string,
  options: ObjectAuthority = {},
) {
  const { has } = useEffectivePermissions(options.groupPermissions)
  return useQuery({
    queryKey: ['conversation', conversationId],
    enabled: evaluationId !== '' && conversationId !== '' && has(READ),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}', {
          params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } },
        }),
      ),
  })
}

export function useMessages(
  evaluationId: string,
  conversationId: string,
  options: ObjectAuthority = {},
) {
  const { has } = useEffectivePermissions(options.groupPermissions)
  return useQuery({
    queryKey: ['messages', conversationId],
    enabled: evaluationId !== '' && conversationId !== '' && has(READ),
    queryFn: async () =>
      unwrap(
        await apiClient.GET(
          '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/messages',
          {
            params: {
              path: { evaluation_id: evaluationId, conversation_id: conversationId },
              query: { limit: 100, offset: 0 },
            },
          },
        ),
      ),
  })
}
