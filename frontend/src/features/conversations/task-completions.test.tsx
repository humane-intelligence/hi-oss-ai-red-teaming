import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { server } from '@/test/msw/server'
import { useMembersCompletedTasks, useToggleTaskCompletion } from './task-completions'

const CONV = 'conv-0001-0000-0000-000000000000'
const OTHER_CONV = 'conv-0002-0000-0000-000000000000'
const TASK = 'task-0001-0000-0000-000000000000'
const KEY = ['completed-tasks', CONV]
const OTHER_KEY = ['completed-tasks', OTHER_CONV]
const url = `http://localhost/api/v1/conversations/${CONV}/completed-tasks/${TASK}`
const listUrl = (conversationId: string) =>
  `http://localhost/api/v1/conversations/${conversationId}/completed-tasks`

// No `useCompletedTasks` observer is mounted, so `onSettled`'s invalidate can't trigger a
// refetch — the cache keeps whatever the optimistic update / rollback left. That isolates
// the rollback: a broken `onError` would leave the optimistic value and fail the assertion.
function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function client() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

describe('useToggleTaskCompletion optimistic rollback', () => {
  it('reverts the optimistic check when the PUT fails', async () => {
    const qc = client()
    qc.setQueryData<string[]>(KEY, []) // task not completed
    server.use(http.put(url, () => HttpResponse.json({ title: 'boom' }, { status: 500 })))

    const { result } = renderHook(() => useToggleTaskCompletion(), { wrapper: wrapper(qc) })
    await act(async () => {
      await result.current
        .mutateAsync({ conversationId: CONV, taskId: TASK, next: true })
        .catch(() => {})
    })

    // Optimistically added TASK, then rolled back on the 500 — so it's gone again.
    expect(qc.getQueryData<string[]>(KEY)).toEqual([])
  })

  it('restores the optimistic uncheck when the DELETE fails', async () => {
    const qc = client()
    qc.setQueryData<string[]>(KEY, [TASK]) // task already completed
    server.use(http.delete(url, () => HttpResponse.json({ title: 'boom' }, { status: 500 })))

    const { result } = renderHook(() => useToggleTaskCompletion(), { wrapper: wrapper(qc) })
    await act(async () => {
      await result.current
        .mutateAsync({ conversationId: CONV, taskId: TASK, next: false })
        .catch(() => {})
    })

    // Optimistically removed TASK, then re-added on the 500 — so it stays completed.
    expect(qc.getQueryData<string[]>(KEY)).toEqual([TASK])
  })

  it('rolls back only the failed task, leaving a concurrent toggle intact', async () => {
    const other = 'task-0002-0000-0000-000000000000'
    const qc = client()
    qc.setQueryData<string[]>(KEY, [])
    server.use(
      http.put(url, () => HttpResponse.json({ title: 'boom' }, { status: 500 })),
      http.put(`http://localhost/api/v1/conversations/${CONV}/completed-tasks/${other}`, () =>
        HttpResponse.json({ id: 'x' }, { status: 200 }),
      ),
    )

    const { result } = renderHook(() => useToggleTaskCompletion(), { wrapper: wrapper(qc) })
    await act(async () => {
      const failing = result.current
        .mutateAsync({ conversationId: CONV, taskId: TASK, next: true })
        .catch(() => {})
      const ok = result.current.mutateAsync({ conversationId: CONV, taskId: other, next: true })
      await Promise.all([failing, ok])
    })

    // The failed task rolled back; the concurrently-succeeding task was not clobbered.
    expect(qc.getQueryData<string[]>(KEY)).toEqual([other])
  })

  it('patches only the toggled conversation, leaving a sibling member untouched', async () => {
    const qc = client()
    qc.setQueryData<string[]>(KEY, [])
    qc.setQueryData<string[]>(OTHER_KEY, [])
    server.use(http.put(url, () => HttpResponse.json({ id: 'x' }, { status: 200 })))

    const { result } = renderHook(() => useToggleTaskCompletion(), { wrapper: wrapper(qc) })
    await act(async () => {
      await result.current.mutateAsync({ conversationId: CONV, taskId: TASK, next: true })
    })

    // The group view checks the same task off per member, so the optimistic patch must
    // land on the target conversation's key only — not every member's.
    expect(qc.getQueryData<string[]>(KEY)).toEqual([TASK])
    expect(qc.getQueryData<string[]>(OTHER_KEY)).toEqual([])
  })
})

describe('useMembersCompletedTasks', () => {
  it('returns each member’s completed task ids aligned by index', async () => {
    server.use(
      http.get(listUrl(CONV), () => HttpResponse.json([{ task_id: TASK }], { status: 200 })),
      http.get(listUrl(OTHER_CONV), () => HttpResponse.json([], { status: 200 })),
    )

    const { result } = renderHook(() => useMembersCompletedTasks([CONV, OTHER_CONV]), {
      wrapper: wrapper(client()),
    })

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.completedTaskIdsByConversation).toEqual([new Set([TASK]), new Set()])
  })

  it('is ready for an empty member list', () => {
    const { result } = renderHook(() => useMembersCompletedTasks([]), {
      wrapper: wrapper(client()),
    })

    // A group whose conversations haven't resolved yet has nothing to wait for, so the
    // rail must not be told the counts are still loading.
    expect(result.current.state).toBe('ready')
    expect(result.current.completedTaskIdsByConversation).toEqual([])
  })

  it('reports error, not ready, when a member read fails', async () => {
    server.use(
      http.get(listUrl(CONV), () => HttpResponse.json([{ task_id: TASK }])),
      http.get(listUrl(OTHER_CONV), () => HttpResponse.json({ title: 'boom' }, { status: 500 })),
    )

    const { result } = renderHook(() => useMembersCompletedTasks([CONV, OTHER_CONV]), {
      wrapper: wrapper(client()),
    })

    // Treating the failed member as "nothing completed" would silently under-report K.
    await waitFor(() => expect(result.current.state).toBe('error'))
  })
})
