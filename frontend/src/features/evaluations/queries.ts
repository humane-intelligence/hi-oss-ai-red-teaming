import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { operations } from '@/lib/api/schema'
import type { EvaluationStatus } from '@/lib/api/types'

export type EvaluationOrderBy = NonNullable<
  operations['list_evaluations_endpoint_api_v1_evaluations_get']['parameters']['query']
>['order_by']

export type EvaluationListParams = {
  limit: number
  offset: number
  search?: string
  status?: EvaluationStatus
  order_by?: EvaluationOrderBy
}

export function useEvaluations(params: EvaluationListParams) {
  return useQuery({
    queryKey: ['evaluations', params],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              search: params.search || undefined,
              status: params.status || undefined,
              order_by: params.order_by,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// Map evaluation id -> title for resolving ids shown in flat lists (scenarios,
// reviews). Gated on evaluations:read so it falls back to a short id rather
// than 403-ing for personas without it (e.g. a reviewer).
export function useEvaluationLookup() {
  const { has } = usePermissions()
  const evaluations = useQuery({
    queryKey: ['evaluation-lookup'],
    enabled: has('evaluations:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations', {
          params: { query: { limit: 100, offset: 0 } },
        }),
      ),
  })
  const byId = new Map<string, string>()
  for (const e of evaluations.data?.items ?? []) byId.set(e.id, e.title)
  return (id: string) => byId.get(id) ?? `${id.slice(0, 8)}…`
}

export function useEvaluation(id: string) {
  return useQuery({
    queryKey: ['evaluation', id],
    enabled: id !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}', {
          params: { path: { evaluation_id: id } },
        }),
      ),
  })
}

// Single-evaluation aggregate metrics — gated server-side by the parent group's configured
// metrics-access level. The group detail injects `evaluation_groups:view_metrics` into
// `user_permissions` whenever the caller would be admitted (full or personal scope), so callers
// pass `enabled` from that key to avoid a 403 (and its error toast). The response's own `scope`
// field says whether the numbers are the full aggregate or the caller's personal slice.
export function useEvaluationMetrics(evaluationId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['evaluation-metrics', evaluationId],
    enabled: evaluationId !== '' && (options?.enabled ?? true),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/metrics', {
          params: { path: { evaluation_id: evaluationId } },
        }),
      ),
  })
}

// Full assignment row including its inference-param overrides — the embedded
// evaluation.models view omits parameters, so the edit dialog fetches this.
// Backend gates this on evaluations:update (an intentional edit-scoped
// read — an assignment is configuration, so it returns the unmasked model_id).
export function useAssignment(evaluationId: string, assignmentId: string) {
  return useQuery({
    queryKey: ['assignment', evaluationId, assignmentId],
    enabled: evaluationId !== '' && assignmentId !== '',
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/models/{assignment_id}', {
          params: { path: { evaluation_id: evaluationId, assignment_id: assignmentId } },
        }),
      ),
  })
}

export function useEvaluationTagKeys(evaluationId: string) {
  return useQuery({
    queryKey: ['evaluation-tag-keys', evaluationId],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/evaluations/{evaluation_id}/tag-keys', {
          params: { path: { evaluation_id: evaluationId } },
        }),
      ),
    enabled: !!evaluationId,
  })
}
