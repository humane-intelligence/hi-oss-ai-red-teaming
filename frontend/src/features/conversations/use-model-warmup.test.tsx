import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { server } from '@/test/msw/server'
import { useModelWarmup, useWarmupGate } from './use-model-warmup'

const EVAL_ID = 'eval-1'
const ASSIGN_ID = 'assign-1'
const URL = `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}/warmup`

// `ready` and `error` are terminal on the first probe, so the loop resolves without
// hitting a backoff sleep — awaiting `warmUp()` is deterministic (no fake timers).
// (The 'starting' → poll path and the transient-error → keep-polling catch are
// covered by the backend `dispatch_probe` classification tests.)
describe('useModelWarmup', () => {
  it('reaches ready when the probe reports ready', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ status: 'ready' })))
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    await act(() => result.current.warmUp())

    expect(result.current.state).toBe('ready')
  })

  it('surfaces error on a terminal probe', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ status: 'error' })))
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    await act(() => result.current.warmUp())

    expect(result.current.state).toBe('error')
  })

  // A terminal HTTP status (e.g. the assignment/model gone) is not transient — the
  // loop must surface it rather than swallowing it and polling to the timeout cap.
  it('surfaces error on a terminal HTTP status instead of polling', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ detail: 'gone' }, { status: 404 })))
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    await act(() => result.current.warmUp())

    expect(result.current.state).toBe('error')
  })
})

// The poll/backoff loop sleeps between probes, so these drive fake timers: each
// `advanceTimersByTimeAsync` fires the next probe and flushes its resolution.
describe('useModelWarmup poll loop', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('keeps polling through "starting" until the probe reports ready', async () => {
    let calls = 0
    server.use(
      http.post(URL, () => {
        calls += 1
        return HttpResponse.json({ status: calls >= 3 ? 'ready' : 'starting' })
      }),
    )
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    act(() => void result.current.warmUp())
    await act(async () => void (await vi.advanceTimersByTimeAsync(0))) // resolve probe 1
    expect(result.current.state).toBe('starting')

    // Backoff is 5s, 5s → probes 2 and 3 fire.
    await act(async () => void (await vi.advanceTimersByTimeAsync(5_000)))
    await act(async () => void (await vi.advanceTimersByTimeAsync(5_000)))

    expect(result.current.state).toBe('ready')
    expect(calls).toBe(3)
  })

  it('treats a 429 / 5xx as transient and keeps polling', async () => {
    let calls = 0
    server.use(
      http.post(URL, () => {
        calls += 1
        if (calls === 1) return HttpResponse.json({ detail: 'rate limited' }, { status: 429 })
        if (calls === 2) return HttpResponse.json({ detail: 'bad gateway' }, { status: 503 })
        return HttpResponse.json({ status: 'ready' })
      }),
    )
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    act(() => void result.current.warmUp())
    await act(async () => void (await vi.advanceTimersByTimeAsync(0))) // 429 → not surfaced as error
    expect(result.current.state).toBe('starting')

    await act(async () => void (await vi.advanceTimersByTimeAsync(5_000))) // 503 → still starting
    await act(async () => void (await vi.advanceTimersByTimeAsync(5_000))) // ready

    expect(result.current.state).toBe('ready')
    expect(calls).toBe(3)
  })

  it('gives up with "timeout" when the endpoint never becomes ready', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ status: 'starting' })))
    const { result } = renderHook(() => useModelWarmup(EVAL_ID, ASSIGN_ID))

    act(() => void result.current.warmUp())
    // Past the 4-minute cap — the loop must stop at 'timeout', not spin forever.
    await act(async () => void (await vi.advanceTimersByTimeAsync(4 * 60_000 + 30_000)))

    expect(result.current.state).toBe('timeout')
  })
})

describe('useWarmupGate', () => {
  it('never probes and never blocks when disabled', () => {
    // No MSW handler is registered — if it probed, `onUnhandledRequest: 'error'` would throw.
    const { result } = renderHook(() => useWarmupGate(EVAL_ID, ASSIGN_ID, false))

    expect(result.current.state).toBe('idle')
    expect(result.current.blocked).toBe(false)
  })

  it('blocks while the model is warming, then unblocks once ready', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ status: 'ready' })))
    const { result } = renderHook(() => useWarmupGate(EVAL_ID, ASSIGN_ID, true))

    // The mount probe fires immediately (optimistic 'starting'), so the chat is blocked.
    expect(result.current.blocked).toBe(true)

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.blocked).toBe(false)
  })

  it('stays blocked when the probe reports a terminal error', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ status: 'error' })))
    const { result } = renderHook(() => useWarmupGate(EVAL_ID, ASSIGN_ID, true))

    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.blocked).toBe(true)
  })
})
