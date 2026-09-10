import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { AnnotationCreate } from '@/lib/api/types'

export function useCreateAnnotation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: AnnotationCreate) =>
      unwrap(await apiClient.POST('/api/v1/annotations', { body })),
    onSuccess: () => {
      // Prefix only: no message or submission payload carries annotation data, so
      // invalidating those would reflash the transcript for nothing.
      qc.invalidateQueries({ queryKey: ['annotations'] })
      // Naming a label creates one, so the picker's own list is stale the moment this
      // succeeds — and "offered again on the next message" is the point of storing it.
      qc.invalidateQueries({ queryKey: ['annotation-labels'] })
    },
    // No toast on *success*: the chip appearing is the feedback, and a picker where each
    // toggle is its own request would otherwise stack one toast per label. A failure still
    // has to speak — the dialog can be closed before it lands, so there is no inline surface
    // to write into (see the delete sibling below).
    onError: () => toast.error('Could not add the label'),
    meta: { suppressErrorToast: true },
  })
}

export function useDeleteAnnotation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/annotations/{annotation_id}', {
          params: { path: { annotation_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['annotations'] })
    },
    // Deliberately no Undo toast, unlike notes: re-picking the label in the same picker
    // recreates it (create is idempotent), so an undo affordance would duplicate the
    // control the user already has open.
    onError: () => toast.error('Could not remove the label'),
    meta: { suppressErrorToast: true },
  })
}
