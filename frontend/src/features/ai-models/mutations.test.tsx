import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useDeleteModel, useRestoreModel, useUpdateModel } from './mutations'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), dismiss: vi.fn() },
}))

const MODEL_ID = 'model-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidated: unknown[] = []
  vi.spyOn(qc, 'invalidateQueries').mockImplementation((filters) => {
    invalidated.push(filters?.queryKey)
    return Promise.resolve()
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { invalidated, wrapper }
}

function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

// Deleting a model unassigns it from every evaluation and group subset and tombstones the
// conversations that ran against those assignments — caches that never mention models.
// Restoring is deliberately shallow, so the two hooks must NOT invalidate the same set.
describe('model delete and restore invalidate exactly what each one changes', () => {
  it('clears every cache the delete cascade touches', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/ai-models/${MODEL_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { invalidated, wrapper } = withClient()
    const { result } = renderHook(() => useDeleteModel(), { wrapper })

    await act(() => result.current.mutateAsync(MODEL_ID))

    // The whole set, asserted as a set: a hand-picked subset would stay green while a
    // key went missing, which is exactly how the group-detail key got lost once.
    await waitFor(() =>
      expect(invalidated).toEqual([
        ['ai-models'],
        // Derived from the live rows, so the delete retires whatever labels only this model wore.
        ['ai-model-labels'],
        ['evaluations'],
        ['evaluation'],
        ['evaluation-groups'],
        ['evaluation-group'],
        ['conversation-groups'],
        ['conversation-group'],
        ['conversations', 'deleted'],
        // The cascade tombstones conversations, and each of these is read through a
        // live one — so they leave the server with the conversations, not on their own.
        ['conversation'],
        ['message-flags'],
        ['notes'],
        ['reviews'],
        ['review-queue'],
        ['completed-tasks'],
      ]),
    )
  })

  it('clears the evaluation caches on edit, since they project the registry row', async () => {
    // `EvaluationAiModelView` carries the model's name, warmup flag and advanced-params
    // opt-out, so an edit that leaves those reads alone lets a New-conversation tab keep
    // offering a parameter panel for a model that no longer accepts one.
    server.use(
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json({ id: MODEL_ID, advanced_params_disabled: true }),
      ),
    )
    const { invalidated, wrapper } = withClient()
    const { result } = renderHook(() => useUpdateModel(MODEL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ advanced_params_disabled: true }))

    await waitFor(() =>
      expect(invalidated).toEqual([
        ['ai-models'],
        ['ai-model-labels'],
        ['ai-model', MODEL_ID],
        ['evaluations'],
        ['evaluation'],
      ]),
    )
  })

  it('clears only the registry caches on restore, since the restore is shallow', async () => {
    server.use(
      http.post(`http://localhost/api/v1/ai-models/${MODEL_ID}/restore`, () =>
        HttpResponse.json({ id: MODEL_ID, deleted_at: null }),
      ),
    )
    const { invalidated, wrapper } = withClient()
    const { result } = renderHook(() => useRestoreModel(), { wrapper })

    await act(() => result.current.mutateAsync(MODEL_ID))

    // The endpoint revives the registry row only — it never re-assigns the model or
    // un-deletes its conversations, so refetching those would be work for nothing. The label
    // vocabulary is the one exception: it reads the live rows, so a revived model's labels are
    // back in it.
    await waitFor(() => expect(invalidated).toEqual([['ai-models'], ['ai-model-labels']]))
  })

  it('undoes the delete exactly once on a double click', async () => {
    let restores = 0
    server.use(
      http.delete(
        `http://localhost/api/v1/ai-models/${MODEL_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/ai-models/${MODEL_ID}/restore`, () => {
        restores += 1
        return HttpResponse.json({ id: MODEL_ID, deleted_at: null })
      }),
    )
    vi.mocked(toast.success).mockClear()
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteModel(), { wrapper })

    await act(() => result.current.mutateAsync(MODEL_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    // Wait for the first restore to land, then let the event loop drain: asserting
    // `toBe(1)` immediately would also pass while a second request was still in flight.
    await waitFor(() => expect(restores).toBe(1))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(restores).toBe(1)
  })
})
