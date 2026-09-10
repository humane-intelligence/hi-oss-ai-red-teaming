import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { PublicPasswordPolicyResponse, PublicPlatformSettingsResponse } from '@/lib/api/types'

// The platform-settings singleton (GET /api/v1/platform-settings, admin-only).
export function usePlatformSettings() {
  return useQuery({
    queryKey: ['platform-settings'],
    queryFn: async () => unwrap(await apiClient.GET('/api/v1/platform-settings')),
  })
}

// The rules the platform ships with; the fallback while the public subset is loading or gone.
const SHIPPED_PASSWORD_POLICY: PublicPasswordPolicyResponse = {
  min_length: 8,
  require_uppercase: false,
  require_digit: false,
  require_symbol: false,
}

// Anonymous-safe subset of the same singleton (GET /api/v1/platform-settings/public, no auth) —
// drives whether the login/register screens offer sign-up and what they say about passwords.
// Fails open on error: the sign-up path stays visible and the password rules read as the shipped
// ones (hiding and tightening are the exceptions, and the backend enforces both regardless of what
// the UI shows), so errors are suppressed rather than toasted at a visitor. While the read is still
// pending `signup_enabled` fails open the same way, but the password callers hold their rendering
// off instead — see `usePasswordPolicy`. The read carries a deadline so that window is bounded.
// `apiClient` sets no timeout, so a hung read would never reach the error branch that falls back to
// the shipped rules, and the requirements list would sit on its placeholder indefinitely. Bounding
// the read is what makes that placeholder transient; with the client's `retry: 1` a hang settles in
// roughly twice this.
const READ_TIMEOUT_MS = 5_000

function usePublicPlatformSettings() {
  return useQuery<PublicPlatformSettingsResponse>({
    // Prefix-shares ['platform-settings'] so the settings mutation's invalidation covers it.
    queryKey: ['platform-settings', 'public'],
    queryFn: async ({ signal }) =>
      unwrap(
        await apiClient.GET('/api/v1/platform-settings/public', {
          // Keep the query's own cancellation; add the deadline on top of it.
          signal: AbortSignal.any([signal, AbortSignal.timeout(READ_TIMEOUT_MS)]),
        }),
      ),
    meta: { suppressErrorToast: true },
  })
}

export function useSignupEnabled(): boolean {
  return usePublicPlatformSettings().data?.signup_enabled ?? true
}

// `isPending` is part of the answer, not a detail: the fallback is indistinguishable from a real
// shipped-policy read, so a caller that rendered the rules before the query settles would state the
// looser policy and then flip. Validation deliberately does not wait: the callers build their
// schema from the fallback while pending, so a submit in that window is checked against the looser
// rules and a password the configured policy refuses comes back as a 400 on the field — cheaper
// than disabling an anonymous recovery screen for as long as the read hangs.
export function usePasswordPolicy(): { policy: PublicPasswordPolicyResponse; isPending: boolean } {
  const query = usePublicPlatformSettings()
  return {
    policy: query.data?.password_policy ?? SHIPPED_PASSWORD_POLICY,
    isPending: query.isPending,
  }
}
