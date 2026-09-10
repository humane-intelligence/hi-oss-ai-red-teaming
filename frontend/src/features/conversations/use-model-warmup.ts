import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { ApiError } from '@/lib/api/problem'

// Poll cadence for a scale-to-zero endpoint waking up: quick at first, then back
// off, capping total wait so a stuck endpoint doesn't spin forever.
const POLL_BACKOFF_MS = [5_000, 5_000, 10_000, 15_000] as const
const MAX_ELAPSED_MS = 4 * 60_000

// 'starting' = still waking (keep polling); the rest are terminal (stop the loop).
export type WarmupState = 'idle' | 'starting' | 'ready' | 'error' | 'timeout'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Probe a model assignment's endpoint and poll until it is ready (or gives up).
 *
 * Imperative like `useConversationStream` — not a TanStack query, since it drives
 * a poll loop rather than caching server state. The first probe also *wakes* a
 * scaled-to-zero endpoint, so calling `warmUp()` on mount doubles as the trigger.
 */
export function useModelWarmup(evaluationId: string, assignmentId: string) {
  const [state, setState] = useState<WarmupState>('idle')
  // A monotonic token, not a boolean flag: each `warmUp()` (and `cancel()`) bumps
  // it, so any earlier loop sees its token superseded and exits — safe under React
  // strict-mode's double-invoke and rapid re-fires without two loops racing.
  const runId = useRef(0)

  const warmUp = useCallback(async () => {
    const myRun = (runId.current += 1)
    setState('starting')
    const start = performance.now()
    let attempt = 0
    while (runId.current === myRun) {
      let status: WarmupState
      try {
        const result = unwrap(
          await apiClient.POST(
            '/api/v1/evaluations/{evaluation_id}/models/{assignment_id}/warmup',
            {
              params: { path: { evaluation_id: evaluationId, assignment_id: assignmentId } },
            },
          ),
        )
        status = result.status
      } catch (err) {
        // A terminal client error (gone/disabled model, forbidden, unauthorized)
        // won't fix itself — surface it now rather than spinning to the cap. 5xx /
        // network / rate-limit are transient: treat as still starting and keep polling.
        if (err instanceof ApiError && err.status < 500 && err.status !== 429) {
          if (runId.current === myRun) setState('error')
          return
        }
        status = 'starting'
      }
      if (runId.current !== myRun) return // superseded/cancelled while awaiting
      if (status === 'ready' || status === 'error') {
        setState(status)
        return
      }
      if (performance.now() - start >= MAX_ELAPSED_MS) {
        setState('timeout')
        return
      }
      // Still 'starting' (already the current state) — just keep polling.
      // `?? ` only satisfies noUncheckedIndexedAccess — the clamped index is always in range.
      await sleep(POLL_BACKOFF_MS[Math.min(attempt, POLL_BACKOFF_MS.length - 1)] ?? 15_000)
      attempt += 1
    }
  }, [evaluationId, assignmentId])

  // Stop any in-flight poll (unmount, or the user navigating away).
  const cancel = useCallback(() => {
    runId.current += 1
  }, [])

  return { state, warmUp, cancel }
}

/**
 * Drive warmup for an assignment and report whether the chat should be blocked.
 *
 * When `enabled` (the model's endpoint is scale-to-zero), probes on mount to wake
 * it and `blocked` stays true until the probe reports `ready` — so the composer can
 * refuse to send into a cold endpoint that would just reject the first message.
 * When not enabled, it never probes and never blocks (always-on models chat freely).
 */
export function useWarmupGate(evaluationId: string, assignmentId: string, enabled: boolean) {
  const { state, warmUp, cancel } = useModelWarmup(evaluationId, assignmentId)

  useEffect(() => {
    if (!enabled) return
    void warmUp()
    return cancel
  }, [enabled, warmUp, cancel])

  return { state, warmUp, blocked: enabled && state !== 'ready' }
}
