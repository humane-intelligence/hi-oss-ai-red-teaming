import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { ApiError } from '@/lib/api/problem'
import type { TermsDocumentResponse } from '@/lib/api/types'

// The version every account is asked to accept (GET /api/v1/terms/current, unauthenticated).
// A 404 is the shipped state, not a failure: nothing published means nothing to accept, so it
// resolves to `null` — both the acceptance gate and the signup checkboxes key off that. Any other
// failure stays an error, so the gate can say so rather than wave the user through. Not toasted:
// every caller renders its own message.
export function useCurrentTerms() {
  return useQuery<TermsDocumentResponse | null>({
    queryKey: ['terms', 'current'],
    queryFn: async () => {
      try {
        // `no-store` defeats the endpoint's own `Cache-Control: max-age=30`. Without it a
        // refetch — including the one recovering from a 409 — can be answered from the browser
        // cache with the very version that was just superseded, and loop there for 30 seconds.
        return unwrap(await apiClient.GET('/api/v1/terms/current', { cache: 'no-store' }))
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }
    },
    // Every caller uses this to make or display a consent decision, so unlike the rest of the app
    // it must not serve the global 30s-stale window: a superseded document on the gate cannot be
    // accepted (the backend 409s on a stale id), and with `refetchOnWindowFocus: false` there is
    // no second chance to correct it — it sat there until a manual reload.
    staleTime: 0,
    refetchOnMount: 'always',
    meta: { suppressErrorToast: true },
  })
}

// The published history (GET /api/v1/terms, `platform_settings:read`) — the admin surface only.
// One page is the whole history in practice; a platform publishes terms a handful of times.
export function useTermsVersions() {
  return useQuery({
    queryKey: ['terms', 'list'],
    // The section renders its own alert for a failed history read, so the global toast would be a
    // second report of the same thing — as with `useCurrentTerms` above.
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/terms', {
          params: { query: { limit: 100, offset: 0 } },
        }),
      ),
  })
}

// One version by id (GET /api/v1/terms/{id}) — what an account actually accepted, which is not
// necessarily the current text. Immutable once published, so this one keeps the global stale
// window; `enabled` off means no acceptance to look up.
export function useTermsDocument(id: string) {
  return useQuery({
    queryKey: ['terms', 'document', id],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/terms/{terms_id}', { params: { path: { terms_id: id } } }),
      ),
    enabled: id !== '',
    meta: { suppressErrorToast: true },
  })
}
