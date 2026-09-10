import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { SavedViewCreate, SavedViewResource, SavedViewUpdate } from '@/lib/api/types'

export function useCreateSavedView(resource: SavedViewResource) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: SavedViewCreate) =>
      unwrap(await apiClient.POST('/api/v1/saved-views', { body })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['saved-views', resource] })
      toast.success('View saved')
    },
  })
}

export function useUpdateSavedView(resource: SavedViewResource) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body: SavedViewUpdate }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/saved-views/{view_id}', {
          params: { path: { view_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['saved-views', resource] })
      toast.success('View updated')
    },
  })
}

export function useRestoreSavedView(resource: SavedViewResource) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/saved-views/{view_id}/restore', {
          params: { path: { view_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['saved-views', resource] })
      toast.success('View restored')
    },
  })
}

export function useDeleteSavedView(resource: SavedViewResource) {
  const qc = useQueryClient()
  const restore = useRestoreSavedView(resource)
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/saved-views/{view_id}', {
          params: { path: { view_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['saved-views', resource] })
      let undone = false
      toast.success('View deleted', {
        // Views live in a dropdown with no list surface, so there is nowhere to browse
        // tombstones — this toast is the only way back, unlike the other entities.
        description: 'Undo here is the only way back in the console.',
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a
          // fast second click would restore an already-live row and toast its 404.
          // Released again on failure: the restore 409s when a live view has taken
          // the name back, and this toast is the only way back for a saved view.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(id, { onError: () => (undone = false) })
          },
        },
      })
    },
  })
}
