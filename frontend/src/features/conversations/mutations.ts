import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { humanizeError } from '@/lib/api/problem'
import { restorableFromHint } from '@/lib/restore/restore'
import type {
  ConversationCreate,
  ConversationGroupCreate,
  ConversationUpdate,
} from '@/lib/api/types'

export function useRestoreConversation(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (conversationId: string) =>
      unwrap(
        await apiClient.POST(
          '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/restore',
          { params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } } },
        ),
      ),
    onSuccess: (conversation) => {
      qc.invalidateQueries({ queryKey: ['conversation', conversation.id] })
      qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      qc.invalidateQueries({ queryKey: ['conversation-group', conversation.conversation_group_id] })
      // Flags, notes, reviews and task completions are all read through a live
      // conversation, so they come back with it.
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['notes'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['completed-tasks'] })
      toast.success('Conversation restored')
    },
  })
}

// Undo of a group delete, which cascaded to every member. Sequential rather than
// parallel: each restore revives the shared group, and one summary toast replaces the
// N per-restore ones — a partial failure otherwise reported several successes and a
// separate error, naming no member.
export function useRestoreConversations(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (conversationIds: string[]) => {
      const failures: string[] = []
      for (const conversationId of conversationIds) {
        try {
          await unwrap(
            await apiClient.POST(
              '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/restore',
              {
                params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } },
              },
            ),
          )
        } catch (error) {
          failures.push(humanizeError(error))
        }
      }
      return { total: conversationIds.length, failures }
    },
    onSuccess: ({ total, failures }, conversationIds) => {
      qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      qc.invalidateQueries({ queryKey: ['conversation-group'] })
      // `['conversation-group']` does not prefix-match `['conversation', id]`, so each
      // member's detail needs its own key — the single restore does the same at :24.
      for (const id of conversationIds) qc.invalidateQueries({ queryKey: ['conversation', id] })
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['notes'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['completed-tasks'] })
      if (failures.length === 0) {
        toast.success(total === 1 ? 'Conversation restored' : `${total} conversations restored`)
        return
      }
      toast.error(`Restored ${total - failures.length} of ${total} conversations`, {
        description: failures[0],
      })
    },
  })
}

export function useDeleteConversation(evaluationId: string) {
  const qc = useQueryClient()
  const restore = useRestoreConversation(evaluationId)
  return useMutation({
    mutationFn: async (conversationId: string) =>
      unwrap(
        await apiClient.DELETE(
          '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}',
          { params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } } },
        ),
      ),
    onSuccess: (_data, conversationId) => {
      qc.invalidateQueries({ queryKey: ['conversation', conversationId] })
      qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      // A 204 carries no group id, so the whole key goes — the group detail embeds
      // its members and would otherwise still list this one as live.
      qc.invalidateQueries({ queryKey: ['conversation-group'] })
      // Everything that reads through a live conversation drops server-side with it, with
      // no cascade rows involved: flags and notes (`join_conversation_scoped`), the review
      // queue and its submissions (`scope_reviews` / `scope_review_flags`), task completions.
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['notes'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['completed-tasks'] })
      let undone = false
      toast.success('Single conversation deleted', {
        description: restorableFromHint("the evaluation's Recently deleted list"),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a
          // fast second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(conversationId)
          },
        },
      })
    },
  })
}

// The scenario is picked on this form, so it rides the mutate variables; the sibling
// `useAddConversationToGroup` takes it as a hook arg because the group already fixes it.
export function useCreateConversationGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      scenarioId,
      body,
    }: {
      scenarioId: string
      body: ConversationGroupCreate
    }) =>
      unwrap(
        await apiClient.POST('/api/v1/scenarios/{scenario_id}/conversation-groups', {
          params: { path: { scenario_id: scenarioId } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      toast.success('Conversation started')
    },
  })
}

export function useRenameConversation(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      conversationId,
      title,
    }: {
      conversationId: string
      title: string | null
    }) =>
      unwrap(
        await apiClient.PATCH(
          '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}',
          {
            params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } },
            body: { title },
          },
        ),
      ),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ['conversation', updated.id] })
      qc.invalidateQueries({ queryKey: ['conversation-group'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      toast.success('Single conversation renamed')
    },
  })
}

export function useUpdateConversationTags(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    // The Edit-tags dialog renders every failure inline, including the policy 400 that names the
    // offending key — a toast on top would report the same thing twice.
    meta: { suppressErrorToast: true },
    mutationFn: async ({
      conversationId,
      tags,
    }: {
      conversationId: string
      tags: NonNullable<ConversationUpdate['tags']>
    }) =>
      unwrap(
        await apiClient.PATCH(
          '/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}',
          {
            params: { path: { evaluation_id: evaluationId, conversation_id: conversationId } },
            body: { tags },
          },
        ),
      ),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ['conversation', updated.id] })
      qc.invalidateQueries({ queryKey: ['conversation-group'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      toast.success('Tags updated')
    },
  })
}

export function useRenameConversationGroup(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ groupId, name }: { groupId: string; name: string }) =>
      unwrap(
        await apiClient.PATCH(
          '/api/v1/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}',
          {
            params: { path: { evaluation_id: evaluationId, conversation_group_id: groupId } },
            body: { name },
          },
        ),
      ),
    onSuccess: (group) => {
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      qc.invalidateQueries({ queryKey: ['conversation-group', group.id] })
      toast.success('Conversation renamed')
    },
  })
}

export function useAddConversationToGroup(scenarioId: string, groupId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ConversationCreate) =>
      unwrap(
        await apiClient.POST('/api/v1/scenarios/{scenario_id}/conversations', {
          params: { path: { scenario_id: scenarioId } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['conversation-group', groupId] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      toast.success('Model added')
    },
  })
}

// Deleting a group cascades to its member conversations, and there is no group-level
// restore endpoint — but each cascaded conversation is individually restorable, and
// restoring one revives its group. So Undo replays the members: pass the ids that were
// live at delete time (`group.conversations` is loaded live-only) and the group comes
// back around them.
export function useDeleteConversationGroup(evaluationId: string) {
  const qc = useQueryClient()
  const restoreAll = useRestoreConversations(evaluationId)
  return useMutation({
    mutationFn: async ({ groupId }: { groupId: string; conversationIds: string[] }) =>
      unwrap(
        await apiClient.DELETE(
          '/api/v1/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}',
          { params: { path: { evaluation_id: evaluationId, conversation_group_id: groupId } } },
        ),
      ),
    onSuccess: (_data, { groupId, conversationIds }) => {
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      qc.invalidateQueries({ queryKey: ['conversation-group', groupId] })
      qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
      for (const id of conversationIds) qc.invalidateQueries({ queryKey: ['conversation', id] })
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['notes'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['completed-tasks'] })
      let undone = false
      toast.success('Conversation deleted', {
        description: restorableFromHint("the evaluation's Recently deleted list"),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, and this
          // replay is N requests — a second click would double every one of them.
          onClick: () => {
            if (undone) return
            undone = true
            restoreAll.mutate(conversationIds)
          },
        },
      })
    },
  })
}
