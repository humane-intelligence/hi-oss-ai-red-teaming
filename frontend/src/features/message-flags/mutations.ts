import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { MessageFlagCreate } from '@/lib/api/types'

export function useCreateFlag(conversationId?: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: MessageFlagCreate) =>
      unwrap(await apiClient.POST('/api/v1/message-flags', { body })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      if (conversationId) qc.invalidateQueries({ queryKey: ['messages', conversationId] })
      toast.success('Message flagged')
    },
  })
}

export function useUpdateFlag(flagId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { reason?: string; comment?: string | null; red_flagged?: boolean }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/message-flags/{flag_id}', {
          params: { path: { flag_id: flagId } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['message-flag', flagId] })
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      toast.success('Flag updated')
    },
  })
}

export function useRestoreFlag() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (flagId: string) =>
      unwrap(
        await apiClient.POST('/api/v1/message-flags/{flag_id}/restore', {
          params: { path: { flag_id: flagId } },
        }),
      ),
    onSuccess: (flag) => {
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['message-flag', flag.id] })
      qc.invalidateQueries({ queryKey: ['messages', flag.conversation_id] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      toast.success('Flag restored')
    },
  })
}

export function useDeleteFlag() {
  const qc = useQueryClient()
  const restore = useRestoreFlag()
  return useMutation({
    mutationFn: async (flagId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/message-flags/{flag_id}', {
          params: { path: { flag_id: flagId } },
        }),
      ),
    onSuccess: (_data, flagId) => {
      qc.invalidateQueries({ queryKey: ['message-flags'] })
      qc.invalidateQueries({ queryKey: ['message-flag', flagId] })
      // A 204 carries no conversation id, so the whole key goes: the per-message
      // `flag_count` badge and the review queue both drop a deleted flag.
      qc.invalidateQueries({ queryKey: ['messages'] })
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      let undone = false
      toast.success('Flag deleted', {
        description: restorableFromHint('Recently deleted on My flags'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a
          // fast second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(flagId)
          },
        },
      })
    },
  })
}
