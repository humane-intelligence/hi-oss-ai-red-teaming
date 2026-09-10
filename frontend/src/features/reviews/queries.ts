import { useState } from 'react'
import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { AssignableReviewerResponse, ReviewStatus } from '@/lib/api/types'
import type { operations } from '@/lib/api/schema'

export function useReviewQueue(params: { limit: number; offset: number; unassigned?: boolean }) {
  return useQuery({
    queryKey: ['review-queue', params],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/review-queue', {
          // Off is sent as absent, so the unfiltered queue keeps the URL it has always had.
          params: { query: { ...params, unassigned: params.unassigned || undefined } },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// One submission for the reviewer: flag body + reviews + superseded ids, in one
// review-scoped call (replaces the owner-scoped flag + reviews pair on the detail page).
export function useSubmission(submissionId: string) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['submission', submissionId],
    enabled: submissionId !== '' && has('reviews:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/submissions/{submission_id}', {
          params: { path: { submission_id: submissionId } },
        }),
      ),
  })
}

// The submission's full parent transcript, review-scoped. Loaded whole (one extra
// fetch sized to `total`) so every flagged message is present and anchorable.
export function useSubmissionMessages(submissionId: string) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['submission-messages', submissionId],
    enabled: submissionId !== '' && has('reviews:read'),
    queryFn: async () => {
      const path = { submission_id: submissionId }
      const first = unwrap(
        await apiClient.GET('/api/v1/submissions/{submission_id}/messages', {
          params: { path, query: { limit: 100, offset: 0 } },
        }),
      )
      if (first.total <= first.items.length) return first
      return unwrap(
        await apiClient.GET('/api/v1/submissions/{submission_id}/messages', {
          params: { path, query: { limit: first.total, offset: 0 } },
        }),
      )
    },
  })
}

export type ReviewOrderBy = NonNullable<
  operations['list_reviews_endpoint_api_v1_reviews_get']['parameters']['query']
>['order_by']

export function useReviews(params: {
  limit: number
  offset: number
  status?: ReviewStatus
  reviewer_id?: string
  deleted?: boolean
  order_by?: ReviewOrderBy
}) {
  return useQuery({
    queryKey: ['reviews', params],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/reviews', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              status: params.status || undefined,
              reviewer_id: params.reviewer_id || undefined,
              // Needs no permission of its own: the listing returns the caller's own
              // unassignments (every actor's for a break-glass manager).
              deleted: params.deleted,
              order_by: params.order_by,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

// The candidate pool for the "Assign reviewer" dialog: exactly the users the
// assign POST accepts (group annotator pool minus author + already-assigned), so
// the dropdown never offers a choice the backend rejects with a 404.
// Factored out so the single-flag hook and the multi-flag union hook share one key + fetch shape.
export function assignableReviewersQueryOptions(
  flagId: string,
  opts?: { enabled?: boolean; search?: string },
) {
  return {
    queryKey: ['assignable-reviewers', flagId, opts?.search ?? ''],
    enabled: (opts?.enabled ?? true) && flagId !== '',
    placeholderData: keepPreviousData,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/submissions/{submission_id}/assignable-reviewers', {
          params: {
            path: { submission_id: flagId },
            query: { limit: 20, offset: 0, search: opts?.search || undefined },
          },
        }),
      ),
  }
}

export function useAssignableReviewers(
  flagId: string,
  opts?: { enabled?: boolean; search?: string },
) {
  return useQuery(assignableReviewersQueryOptions(flagId, opts))
}

// Tied to the generated response, but only the fields the union reads: a contract rename breaks
// compilation here, while fixtures stay free of fields this merge never touches.
type PoolMember = Pick<AssignableReviewerResponse, 'id' | 'email' | 'active_review_count'>

type AssignablePoolResult = { flagId: string; items: PoolMember[] }

// Union of several flags' assignable pools for the picker, plus a per-flag eligibility map so the
// cartesian can be pre-filtered to pairs the backend will accept (races still surface as per-row 409).
export function mergeAssignablePools(results: AssignablePoolResult[]): {
  options: { value: string; label: string; hint: string }[]
  poolByFlag: Map<string, Set<string>>
} {
  const labelByValue = new Map<string, string>()
  const hintByValue = new Map<string, string>()
  const poolByFlag = new Map<string, Set<string>>()
  for (const { flagId, items } of results) {
    const set = new Set<string>()
    for (const u of items) {
      labelByValue.set(u.id, u.email)
      // Global, so every flag's pool reports the same number for the same person.
      const n = u.active_review_count
      hintByValue.set(u.id, `${n} active review${n === 1 ? '' : 's'}`)
      set.add(u.id)
    }
    poolByFlag.set(flagId, set)
  }
  return {
    options: [...labelByValue].map(([value, label]) => ({
      value,
      label,
      hint: hintByValue.get(value) ?? '',
    })),
    poolByFlag,
  }
}

// One query per selected flag (shared debounced search); union for the picker.
export function useUnionAssignableReviewers(
  flagIds: string[],
  opts?: { enabled?: boolean; search?: string },
) {
  const queries = useQueries({
    queries: flagIds.map((flagId) => assignableReviewersQueryOptions(flagId, opts)),
  })

  // Accumulate each flag's eligible reviewers across search terms — but ONLY to back `poolByFlag`
  // (the cartesian pre-filter), never the visible `options` (built from the current search below).
  // Without this a reviewer picked under an earlier search would be silently dropped from the POST once
  // the current search narrowed them out of the page-limited result set, though their chip still reads
  // selected. The pre-filter is a best-effort optimization; the backend per-row check stays authoritative.
  // Derived state updated during render (converges via a size guard); flags no longer selected are
  // pruned so a reopen for different flags starts clean.
  const [seen, setSeen] = useState<Map<string, Map<string, PoolMember>>>(() => new Map())
  const nextSeen = new Map(seen)
  let changed = false
  flagIds.forEach((flagId, i) => {
    const acc = new Map(nextSeen.get(flagId))
    const before = acc.size
    for (const u of queries[i]?.data?.items ?? []) acc.set(u.id, u)
    if (!nextSeen.has(flagId) || acc.size !== before) {
      nextSeen.set(flagId, acc)
      changed = true
    }
  })
  for (const flagId of [...nextSeen.keys()]) {
    if (!flagIds.includes(flagId)) {
      nextSeen.delete(flagId)
      changed = true
    }
  }
  if (changed) setSeen(nextSeen)
  const accumulated = changed ? nextSeen : seen

  // poolByFlag from the ACCUMULATED pool (the pre-filter must not forget a picked reviewer the current
  // search narrowed out); options from the CURRENT search only, so the visible list narrows as you type.
  const { poolByFlag } = mergeAssignablePools(
    flagIds.map((flagId) => ({
      flagId,
      items: [...(accumulated.get(flagId) ?? new Map<string, PoolMember>()).values()],
    })),
  )
  const { options } = mergeAssignablePools(
    flagIds.map((flagId, i) => ({ flagId, items: queries[i]?.data?.items ?? [] })),
  )
  return {
    options,
    poolByFlag,
    isPending: queries.some((q) => q.isPending),
    isError: queries.some((q) => q.isError),
  }
}
