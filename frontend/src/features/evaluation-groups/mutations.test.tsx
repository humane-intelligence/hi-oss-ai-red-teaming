import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { usePublishGroup, useSubmitGroup } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const GROUP_ID = 'grp-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { spy, wrapper }
}

function refuse(transition: string) {
  server.use(
    http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/${transition}`, () =>
      HttpResponse.json({ title: 'Bad Request', status: 400 }, { status: 400 }),
    ),
  )
}

// A refusal is exactly when the cached `publication_blockers` are known to be wrong: the
// server just listed gaps the page shows as none. With a 30s staleTime and no
// refetch-on-focus, invalidating only on success leaves the button enabled and the
// readiness panel empty against a 400 that says otherwise.
describe('a refused transition still refreshes the group detail', () => {
  it('invalidates after a 400 on submit', async () => {
    refuse('submit')
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useSubmitGroup(GROUP_ID), { wrapper })

    await act(async () => {
      await result.current.mutateAsync().catch(() => {})
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ['evaluation-group', GROUP_ID] }),
      ),
    )
  })

  it('invalidates after a 400 on publish', async () => {
    refuse('publish')
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => usePublishGroup(GROUP_ID), { wrapper })

    await act(async () => {
      await result.current.mutateAsync().catch(() => {})
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ['evaluation-group', GROUP_ID] }),
      ),
    )
  })
})
