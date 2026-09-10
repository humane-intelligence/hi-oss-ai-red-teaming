import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useAssignModel, useUnassignModel } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const MODEL_ID = 'model-001-0000-0000-000000000000'
const ASSIGN_ID = 'asgn-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, spy, wrapper }
}

// The assign picker lists assignable_to_evaluation (subset minus assigned), so
// changing the assignment set must refresh the ['ai-models'] cache.
describe('evaluation model assignment invalidates the assignable-models cache', () => {
  it('refreshes ai-models after assigning', async () => {
    server.use(
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models`, () =>
        HttpResponse.json(
          { id: ASSIGN_ID, evaluation_id: EVAL_ID, model_id: MODEL_ID },
          { status: 201 },
        ),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useAssignModel(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ model_id: MODEL_ID }))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['ai-models'] })),
    )
  })

  it('refreshes ai-models after unassigning', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useUnassignModel(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync(ASSIGN_ID))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['ai-models'] })),
    )
  })
})

// Unassigning is the one console delete that reaches conversations, so it is the one that
// most needs an Undo — and the restore endpoint for it shipped in this same change.
describe('undoing a model unassign', () => {
  it('re-assigns the model and refreshes the conversation caches', async () => {
    let restored: string | null = null
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}/restore`,
        () => {
          restored = ASSIGN_ID
          return HttpResponse.json({
            id: ASSIGN_ID,
            evaluation_id: EVAL_ID,
            model_id: MODEL_ID,
            deleted_at: null,
          })
        },
      ),
    )
    vi.mocked(toast.success).mockClear()
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useUnassignModel(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync(ASSIGN_ID))
    // The unassign soft-deletes every conversation against the assignment, so the group
    // detail and the completions read through those conversations are both stale the moment
    // it succeeds.
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['conversation-group'] }))
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['completed-tasks'] }))

    const calls = vi.mocked(toast.success).mock.calls
    const [, options] = calls[calls.length - 1] as unknown as [
      string,
      { action: { onClick: () => void } },
    ]
    await act(async () => options.action.onClick())

    await waitFor(() => expect(restored).toBe(ASSIGN_ID))
  })
})
