import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { PlatformSettingsUpdate } from '@/lib/api/types'

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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['platform-settings'] })
      for (const queryKey of LICENSE_DERIVED_KEYS) qc.invalidateQueries({ queryKey })
      toast.success('Settings saved')
    },
  })
}
