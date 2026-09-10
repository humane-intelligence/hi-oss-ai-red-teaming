import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { LicenseCreate, LicenseUpdate } from '@/lib/api/types'

function useInvalidateLicense(id?: string) {
  const qc = useQueryClient()
  return () => {
    // Prefix-invalidates the picker (['licenses']), the admin list, and the
    // recently-deleted list (['licenses', 'deleted']) — a delete or restore moves a row
    // between the last two.
    qc.invalidateQueries({ queryKey: ['licenses'] })
    if (id) qc.invalidateQueries({ queryKey: ['license', id] })
  }
}

export function useCreateLicense() {
  const invalidate = useInvalidateLicense()
  return useMutation({
    mutationFn: async (body: LicenseCreate) =>
      unwrap(await apiClient.POST('/api/v1/licenses', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('License created')
    },
  })
}

export function useUpdateLicense(id: string) {
  const invalidate = useInvalidateLicense(id)
  return useMutation({
    mutationFn: async (body: LicenseUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/licenses/{license_id}', {
          params: { path: { license_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('License updated')
    },
  })
}

export function useRestoreLicense() {
  const invalidate = useInvalidateLicense()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/licenses/{license_id}/restore', {
          params: { path: { license_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('License restored')
    },
  })
}

export function useDeleteLicense() {
  const invalidate = useInvalidateLicense()
  const restore = useRestoreLicense()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/licenses/{license_id}', {
          params: { path: { license_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate()
      let undone = false
      toast.success('License deleted', {
        description: restorableFromHint('Recently deleted on Data licenses'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(id)
          },
        },
      })
    },
  })
}
