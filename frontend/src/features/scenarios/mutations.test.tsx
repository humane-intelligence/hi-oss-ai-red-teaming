import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useCreateScenario, useDeleteScenario } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const SCENARIO_ID = 'scen-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { spy, wrapper }
}

// The group detail's `publication_blockers` are derived from the scenario set, so adding the
// last missing scenario is exactly what enables Publish. With a 30s staleTime and no
// refetch-on-focus, a group page that isn't invalidated here keeps Publish disabled.
describe('scenario writes invalidate the group detail', () => {
  it('refreshes the group after creating a scenario', async () => {
    server.use(
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({ id: SCENARIO_ID, evaluation_id: EVAL_ID, name: 's' }, { status: 201 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useCreateScenario(EVAL_ID), { wrapper })

    await act(() =>
      result.current.mutateAsync({ name: 's', description: 'd', required_reviews: 1 }),
    )

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['evaluation-group'] })),
    )
  })

  it('refreshes the group after deleting one', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/${SCENARIO_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useDeleteScenario(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync(SCENARIO_ID))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['evaluation-group'] })),
    )
  })
})
