import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'

export function useAiModels(
  params: {
    limit: number
    offset: number
    for_group?: string
    assignable_to_evaluation?: string
    deleted?: boolean
  },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['ai-models', params],
    enabled: options?.enabled ?? true,
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/ai-models', { params: { query: params } })),
    placeholderData: keepPreviousData,
  })
}

// The suggestion set for the label picker. Unfiltered and unpaginated by design (a picker needs the
// whole vocabulary), so the search box narrows it client-side.
export function useAiModelLabels() {
  return useQuery({
    queryKey: ['ai-model-labels'],
    queryFn: async () => unwrap(await apiClient.GET('/api/v1/ai-models/labels', {})),
    // The picker renders the failure itself ("Could not load results."), and a lost vocabulary costs
    // the operator suggestions, not the field — they can still type a label. One channel, per the
    // repo's one-failure-reported-once rule.
    meta: { suppressErrorToast: true },
  })
}

// Once a check has been in flight this long, back OFF the poll rather than stopping — a
// never-settling `checking` (worker down) shouldn't hammer every 2s while the page stays open,
// but a slow cold start must still get its settling poll. Mirrors the export pollers.
const HEALTH_POLL_BACKOFF_MS = 60_000

export function useAiModel(id: string) {
  return useQuery({
    queryKey: ['ai-model', id],
    enabled: id !== '',
    // A transient blip on a poll shouldn't pop a global error toast — the detail page renders
    // load failures inline. Mirrors the export pollers (see lib/query.ts).
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/ai-models/{model_id}', {
          params: { path: { model_id: id } },
        }),
      ),
    // A health check runs async on the worker; poll while it's in flight so the settled
    // outcome (alive/dead) appears on its own, then stop. Back off past the expected wait.
    refetchInterval: (query) => {
      const model = query.state.data
      if (model?.health_check_status !== 'checking') return false
      const ageMs = model.last_health_check_at
        ? Date.now() - new Date(model.last_health_check_at).getTime()
        : 0
      return ageMs > HEALTH_POLL_BACKOFF_MS ? 15_000 : 2000
    },
  })
}
