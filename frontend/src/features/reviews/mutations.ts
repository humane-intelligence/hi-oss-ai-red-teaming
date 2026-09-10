import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { ReviewBulkRequest, ReviewUpdate } from '@/lib/api/types'

export function useBulkAssignReviewers() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ReviewBulkRequest) =>
      unwrap(await apiClient.POST('/api/v1/reviews/bulk', { body })),
    onSuccess: (res) => {
      for (const key of [['review-queue'], ['reviews'], ['submission'], ['assignable-reviewers']])
        qc.invalidateQueries({ queryKey: key })
      // Pre-filtering makes failures rare (true races only); still surface them rather than lie.
      if (res.failed > 0) toast.warning(`${res.succeeded} assigned, ${res.failed} failed`)
      else toast.success(`${res.succeeded} reviewer assignment(s) created`)
    },
  })
}

export function useRecordVerdict() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ reviewId, body }: { reviewId: string; body: ReviewUpdate }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/reviews/{review_id}', {
          params: { path: { review_id: reviewId } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['reviews'] })
      qc.invalidateQueries({ queryKey: ['submission'] })
      toast.success('Verdict recorded')
    },
  })
}

// Both directions move the same four caches: the queue's completed count, the listings, the
// submission detail, and the candidate pool (an unassign re-qualifies that reviewer, a
// restore disqualifies them again).
function useInvalidateReview() {
  const qc = useQueryClient()
  return () => {
    for (const key of [['review-queue'], ['reviews'], ['submission'], ['assignable-reviewers']])
      qc.invalidateQueries({ queryKey: key })
  }
}

export function useRestoreReview() {
  const invalidate = useInvalidateReview()
  return useMutation({
    mutationFn: async (reviewId: string) =>
      unwrap(
        await apiClient.POST('/api/v1/reviews/{review_id}/restore', {
          params: { path: { review_id: reviewId } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      // The verdict comes back with the assignment, so this is not "re-assigned from scratch".
      toast.success('Reviewer re-assigned', {
        description: 'They are notified by email, as on a fresh assignment.',
      })
    },
  })
}

export function useUnassignReviewer() {
  const invalidate = useInvalidateReview()
  const restore = useRestoreReview()
  return useMutation({
    mutationFn: async (reviewId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/reviews/{review_id}', {
          params: { path: { review_id: reviewId } },
        }),
      ),
    onSuccess: (_data, reviewId) => {
      invalidate()
      let undone = false
      toast.success('Reviewer unassigned', {
        description: restorableFromHint('Recently deleted on Reviews'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would restore an already-live row and toast its 409.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(reviewId, { onError: () => (undone = false) })
          },
        },
      })
    },
  })
}
