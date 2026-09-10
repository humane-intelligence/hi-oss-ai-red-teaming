import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'

export function useScenarios(params: { limit: number; offset: number; search?: string }) {
  return useQuery({
    queryKey: ['scenarios', params],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/scenarios', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              search: params.search || undefined,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

export function useEvaluationScenarios(evaluationId: string) {
  return useQuery({
    queryKey: ['evaluation-scenarios', evaluationId],
    enabled: evaluationId !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/scenarios', {
          params: {
            path: { evaluation_id: evaluationId },
            query: { limit: 100, offset: 0 },
          },
        }),
      ),
  })
}

// One scenario by id, resolving to null on 404 — and only on 404: callers read `null`
// as "the scenario is gone" (the rigor source falls back to `required_reviews` 1, the
// conversation pages render a tombstone note), so swallowing any other status would
// claim deletion on a transient 5xx and skip the global error toast. The 200 embeds
// `tasks`, but the rail reads them via `useScenarioTasks` — only that key is
// invalidated when a task changes.
export function useScenario(evaluationId: string, scenarioId: string | null | undefined) {
  return useQuery({
    queryKey: ['scenario', evaluationId, scenarioId],
    enabled: evaluationId !== '' && scenarioId != null && scenarioId !== '',
    queryFn: async () => {
      const res = await apiClient.GET(
        '/api/v1/evaluations/{evaluation_id}/scenarios/{scenario_id}',
        {
          params: { path: { evaluation_id: evaluationId, scenario_id: scenarioId as string } },
        },
      )
      if (res.response.status === 404) return null
      return unwrap(res)
    },
  })
}

export function useScenarioTasks(scenarioId: string) {
  return useQuery({
    queryKey: ['scenario-tasks', scenarioId],
    enabled: scenarioId !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/scenarios/{scenario_id}/tasks', {
          params: {
            path: { scenario_id: scenarioId },
            query: { limit: 100, offset: 0 },
          },
        }),
      ),
  })
}
