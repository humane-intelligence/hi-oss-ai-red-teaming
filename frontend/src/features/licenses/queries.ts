import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'

// The data-license list (GET /api/v1/licenses) — the picker's source, mounted by the group form, the
// evaluation form, system preferences and the licences list page. Each item carries the mutable
// `is_default` flag (the platform default an admin can change), so this must never become
// `staleTime: Infinity` — that would pin a stale "(platform default)" label until a reload. The
// explicit 30s restates the client-wide default (see lib/query.ts) so a future change there cannot
// silently widen this one; an in-app change is not bound by it at all, because
// `invalidateQueries(['licenses'])` (see system-preferences/mutations.ts) refetches active
// observers regardless. The window only bounds how long an out-of-band change (another admin,
// another session) can go unseen. limit=100 fetches the whole (small) set so the picker sees every
// entry.
export function useLicenses() {
  return useQuery({
    queryKey: ['licenses'],
    staleTime: 30_000,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/licenses', {
          params: { query: { limit: 100, offset: 0 } },
        }),
      ),
  })
}

// The caller's recently-deleted licences — the restore surface. Its own key, so the picker's
// `['licenses']` cache is never displaced by a tombstone page; gated on `licenses:delete`,
// which the endpoint also requires.
export function useDeletedLicenses(enabled: boolean) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['licenses', 'deleted'],
    enabled: enabled && has('licenses:delete'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/licenses', {
          params: { query: { limit: 100, offset: 0, deleted: true } },
        }),
      ),
    // The disclosure renders its failure inline.
    meta: { suppressErrorToast: true },
  })
}

// One data license with its full legal text (GET /api/v1/licenses/{id}) — the detail and edit views,
// and the licence-text dialog, which is why this stays un-gated: the endpoint is auth-only, so a
// reader without any `licenses:*` permission can still read the terms their data is under.
// `suppressErrorToast` is for callers that render the failure themselves.
export function useLicense(id: string, opts?: { suppressErrorToast?: boolean }) {
  return useQuery({
    queryKey: ['license', id],
    meta: opts?.suppressErrorToast ? { suppressErrorToast: true } : undefined,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/licenses/{license_id}', {
          params: { path: { license_id: id } },
        }),
      ),
    enabled: id !== '',
  })
}
