import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useDeleteSavedView } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const VIEW_ID = 'sv-1'

function withClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

// The Undo action lives inside the toast options, and no <Toaster> is mounted in tests,
// so the only way to exercise it is to pull the handler off the mocked toast call.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

function mockDelete() {
  server.use(
    http.delete(
      `http://localhost/api/v1/saved-views/${VIEW_ID}`,
      () => new HttpResponse(null, { status: 204 }),
    ),
  )
}

describe('undoing a saved-view delete', () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  it('fires once when Undo is double-clicked', async () => {
    mockDelete()
    const attempts: string[] = []
    server.use(
      http.post(`http://localhost/api/v1/saved-views/${VIEW_ID}/restore`, () => {
        attempts.push(VIEW_ID)
        return HttpResponse.json({ id: VIEW_ID, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteSavedView('message-flags'), { wrapper })

    await act(() => result.current.mutateAsync(VIEW_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toEqual([VIEW_ID]))
  })

  it('allows a retry after the restore fails', async () => {
    // A saved view's only way back is this toast, so a 409 (a live view took the name)
    // must not burn the Undo — the guard releases and a second click re-attempts.
    mockDelete()
    const attempts: string[] = []
    server.use(
      http.post(`http://localhost/api/v1/saved-views/${VIEW_ID}/restore`, () => {
        attempts.push(VIEW_ID)
        return HttpResponse.json({ title: 'Conflict', status: 409 }, { status: 409 })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteSavedView('message-flags'), { wrapper })

    await act(() => result.current.mutateAsync(VIEW_ID))
    const undo = undoFromLastToast()
    await act(async () => undo())
    await waitFor(() => expect(attempts).toHaveLength(1))
    await act(async () => undo())

    await waitFor(() => expect(attempts).toHaveLength(2))
  })
})
