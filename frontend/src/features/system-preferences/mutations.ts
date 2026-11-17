import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { PlatformSettingsUpdate, PublicPlatformSettingsResponse } from '@/lib/api/types'

// Changing the default license moves the catalog's `is_default` flags and the `effective_license`
// the server resolves through the singleton for every entity that pins none — evaluations, groups
// and both conversation levels carry it. Each is its own key root (detail and list separately), so
// no single prefix reaches them; same set as the group edit in evaluation-groups/mutations.ts.
const LICENSE_DERIVED_KEYS = [
  ['licenses'],
  ['license'],
  ['evaluation'],
  ['evaluations'],
  ['evaluation-group'],
  ['evaluation-groups'],
  ['conversation'],
  ['conversations'],
  ['conversation-group'],
  ['conversation-groups'],
]

export function useUpdatePlatformSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: PlatformSettingsUpdate) =>
      unwrap(await apiClient.PATCH('/api/v1/platform-settings', { body })),
    onSuccess: (saved) => {
      qc.invalidateQueries({ queryKey: ['platform-settings'], exact: true })
      // The public read is shared-cacheable for 30s, so a refetch could get the browser's stale
      // copy back; the saved settings already say what it would return now.
      qc.setQueryData<PublicPlatformSettingsResponse>(
        ['platform-settings', 'public'],
        (prev) =>
          prev && {
            ...prev,
            signup_enabled: !saved.invite_only,
            password_policy: {
              min_length: saved.password_min_length,
              require_uppercase: saved.password_require_uppercase,
              require_digit: saved.password_require_digit,
              require_symbol: saved.password_require_symbol,
            },
            logo_light_key: saved.logo_light_key,
            logo_dark_key: saved.logo_dark_key,
          },
      )
      for (const queryKey of LICENSE_DERIVED_KEYS) qc.invalidateQueries({ queryKey })
      toast.success('Settings saved')
    },
  })
}
