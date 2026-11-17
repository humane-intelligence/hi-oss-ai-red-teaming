import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useBulkAddGroupMembers, usePublishGroup, useSubmitGroup } from './mutations'

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

describe('useBulkAddGroupMembers', () => {
  function bulkResponse(succeeded: number, failed: number) {
    server.use(
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/bulk`, () =>
        HttpResponse.json({
          dry_run: false,
          total: succeeded + failed,
          succeeded,
          failed,
          results: [],
        }),
      ),
    )
  }
  const body = {
    rows: [{ row_key: 'u1', data: { user_id: 'u1', role_ids: ['r1'] } }],
    dry_run: false,
  }

  it('refreshes members and annotators and toasts a clean run', async () => {
    bulkResponse(3, 0)
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useBulkAddGroupMembers(GROUP_ID), { wrapper })

    await act(async () => {
      await result.current.mutateAsync(body)
    })

    expect(spy).toHaveBeenCalledWith({ queryKey: ['evaluation-group-members', GROUP_ID] })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['evaluation-group-annotators', GROUP_ID] })
    expect(toast.success).toHaveBeenCalledWith('3 members added')
  })

  it('leaves a partial failure to the dialog instead of toasting it', async () => {
    vi.mocked(toast.success).mockClear()
    bulkResponse(2, 1)
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useBulkAddGroupMembers(GROUP_ID), { wrapper })

    await act(async () => {
      await result.current.mutateAsync(body)
    })

    expect(spy).toHaveBeenCalledWith({ queryKey: ['evaluation-group-members', GROUP_ID] })
    expect(toast.success).not.toHaveBeenCalled()
  })
})
