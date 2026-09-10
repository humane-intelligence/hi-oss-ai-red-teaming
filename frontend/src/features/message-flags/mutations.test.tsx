import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useDeleteFlag, useRestoreFlag } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const FLAG_ID = 'flag-0001-0000-0000-000000000000'
const CONVERSATION_ID = 'conv-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { spy, wrapper }
}

// A flag's lifecycle moves two caches that don't mention flags: the per-message
// `flag_count` badge on the transcript, and the review queue — which the backend builds
// from live flags only, so a tombstoned one has to leave it.
describe('flag delete and restore invalidate everything they change', () => {
  it('refreshes the messages, queue and reviews caches on delete', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/message-flags/${FLAG_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useDeleteFlag(), { wrapper })

    await act(() => result.current.mutateAsync(FLAG_ID))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['messages'] }))
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['review-queue'] }))
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['reviews'] }))
    })
  })

  it('refreshes the flag’s own conversation on restore', async () => {
    server.use(
      http.post(`http://localhost/api/v1/message-flags/${FLAG_ID}/restore`, () =>
        HttpResponse.json({ id: FLAG_ID, conversation_id: CONVERSATION_ID, deleted_at: null }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useRestoreFlag(), { wrapper })

    await act(() => result.current.mutateAsync(FLAG_ID))

    await waitFor(() => {
      // The response carries the conversation, so restore can narrow where delete can't.
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ['messages', CONVERSATION_ID] }),
      )
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['review-queue'] }))
    })
  })
})
