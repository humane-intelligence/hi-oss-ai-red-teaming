import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError, humanizeError, isConsentRequired } from './api/problem'

// 401 → global redirect; a field-level errors[] (a 422, or a 400 like password policy) is mapped
// onto the form by the caller. Toast the rest.
export function notifyMutationError(error: unknown): void {
  if (
    error instanceof ApiError &&
    (error.status === 401 || error.status === 422 || !!error.problem.errors?.length)
  ) {
    return
  }
  // Handled globally by the acceptance gate, like a 401 — and a toast would land under the
  // success one whenever the write itself is what published the version.
  if (isConsentRequired(error)) return
  toast.error(humanizeError(error))
}

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      // 401 is handled globally (token cleared + redirect to login) — don't double-notify.
      if (error instanceof ApiError && error.status === 401) return
      // Same for the terms refusal: the gate is the one channel, and every mounted query fires
      // this at once when a version lands.
      if (isConsentRequired(error)) return
      // Two kinds of query opt out. Self-polling ones that tolerate transient blips keep
      // polling and surface a terminal state themselves, so a per-poll toast would
      // contradict success. Fanned-out ones (N reads for one surface) would toast N times
      // for a single outage, and the surface reports the failure itself.
      if (query.meta?.suppressErrorToast) return
      toast.error(humanizeError(error))
    },
  }),
  mutationCache: new MutationCache({
    onError: (error, _variables, _onMutateResult, mutation) => {
      // A surface that renders every failure itself opts out, so one failure is reported once —
      // the same contract as the query side, and what `frontend/CLAUDE.md` means by one channel.
      if (mutation.meta?.suppressErrorToast) return
      notifyMutationError(error)
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})
