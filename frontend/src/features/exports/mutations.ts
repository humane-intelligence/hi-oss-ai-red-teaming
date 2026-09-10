import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { ExportJobCreate } from '@/lib/api/types'

// Queue a background export job. Errors (403 non-owner, 409 idempotency-key reuse)
// surface via the global MutationCache toast; the store then tracks the returned job.
export function useCreateExportJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ExportJobCreate) =>
      unwrap(await apiClient.POST('/api/v1/exports/jobs', { body })),
    // Show the new (pending) job in any open detail downloads list right away.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['export-jobs'] }),
  })
}

// Delete a generated export: removes the stored file and soft-deletes the job. On failure (a stray
// 404 if it's already gone, or a 500) the global MutationCache toast fires; on success the list refetches.
export function useDeleteExportJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (jobId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/exports/jobs/{job_id}', {
          params: { path: { job_id: jobId } },
        }),
      ),
    // Refetch the scope's jobs to drop the deleted row; the confirm dialog closes via the caller's onSuccess.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['export-jobs'] })
      toast.success('Export deleted')
    },
  })
}
