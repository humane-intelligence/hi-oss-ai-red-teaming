import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { NoteCreate, NoteUpdate } from '@/lib/api/types'

export function useCreateNote() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: NoteCreate) => unwrap(await apiClient.POST('/api/v1/notes', { body })),
    onSuccess: () => {
      // Prefix only: no message or submission payload carries note data, so
      // invalidating those would reflash the transcript card for nothing.
      qc.invalidateQueries({ queryKey: ['notes'] })
      toast.success('Note added')
    },
    // The dialog renders every failure itself, including the stale-session 403.
    meta: { suppressErrorToast: true },
  })
}

export function useUpdateNote(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: NoteUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/notes/{note_id}', {
          params: { path: { note_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notes'] })
      toast.success('Note updated')
    },
    // Same dialog as create, so the same opt-out: it renders every failure itself.
    meta: { suppressErrorToast: true },
  })
}

export function useRestoreNote() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/notes/{note_id}/restore', {
          params: { path: { note_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notes'] })
      toast.success('Note restored')
    },
  })
}

export function useDeleteNote() {
  const qc = useQueryClient()
  const restore = useRestoreNote()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/notes/{note_id}', {
          params: { path: { note_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['notes'] })
      let undone = false
      toast.success('Note deleted', {
        description: restorableFromHint('Recently deleted notes'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a
          // fast second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(id)
          },
        },
      })
    },
    // No opt-out here, unlike the other two: a confirm dialog has no error surface of
    // its own, so a failed delete needs the global toast to be reported at all.
  })
}
