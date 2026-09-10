import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { VersionResponse } from '@/lib/api/types'

async function fetchVersion(): Promise<VersionResponse> {
  return unwrap(await apiClient.GET('/version'))
}

// Public endpoint (works pre-login). Non-critical: suppress the global error toast
// and let consumers hide the readout when it can't be fetched. Version only moves
// on deploy, so never refetch.
export function useVersion() {
  return useQuery({
    queryKey: ['version'],
    queryFn: fetchVersion,
    staleTime: Infinity,
    meta: { suppressErrorToast: true },
  })
}
