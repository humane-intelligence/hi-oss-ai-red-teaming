import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { useAuth } from '@/lib/auth/auth-context'
import type { TermsPublish } from '@/lib/api/types'

// Accepting returns the same projection as GET /me, so the held identity is adopted straight from
// the response and the gate drops without a second round trip.
export function useAcceptTerms() {
  const { updateUser } = useAuth()
  return useMutation({
    mutationFn: async (termsId: string) =>
      unwrap(await apiClient.POST('/api/v1/auth/me/terms', { body: { terms_id: termsId } })),
    // The gate renders its own failure; it is the whole screen, so it cannot be unmounted
    // underneath the write.
    meta: { suppressErrorToast: true },
    onSuccess: (me) => updateUser(me),
  })
}

// Publishing makes the new version current for *everyone*, the publisher included. The held
// identity carries `terms_acceptance_required` and lives in `AuthProvider` state, not a query, so
// no invalidation can reach it: without re-reading `/me` here the admin who just published would
// keep un-consented access until a full reload, while the confirm dialog promised the opposite.
// Re-reading gates them at once, which is the honest outcome — they have not accepted the text
// they just published either.
export function usePublishTerms() {
  const qc = useQueryClient()
  const { updateUser } = useAuth()
  return useMutation({
    mutationFn: async (body: TermsPublish) =>
      unwrap(await apiClient.POST('/api/v1/terms', { body })),
    meta: { suppressErrorToast: true },
    onSuccess: async (document) => {
      qc.invalidateQueries({ queryKey: ['terms'] })
      toast.success(`Published terms ${document.version}`)
      try {
        updateUser(unwrap(await apiClient.GET('/api/v1/auth/me')))
      } catch {
        // A throw here would fail the whole mutation — the caller would report a failed publish
        // for one that already succeeded, after the success toast. The re-gate is best-effort:
        // without it the publisher keeps un-consented access until the next `/me` read.
        toast.warning('Published. Reload to be asked to accept the new version.')
      }
    },
  })
}
