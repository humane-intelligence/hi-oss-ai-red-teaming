import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useDeleteConversation, useDeleteConversationGroup } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const MEMBER_A = 'conv-0001-0000-0000-000000000000'
const MEMBER_B = 'conv-0002-0000-0000-000000000000'
const MEMBER_C = 'conv-0003-0000-0000-000000000000'
const MEMBERS = [MEMBER_A, MEMBER_B, MEMBER_C]

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { spy, wrapper }
}

// The Undo action lives inside the toast options, and no <Toaster> is mounted in tests,
// so the only way to exercise it is to pull the handler off the mocked toast call.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

function restoreRecorder() {
  const restored: string[] = []
  server.use(
    http.post(
      `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/:conversationId/restore`,
      ({ params }) => {
        restored.push(params.conversationId as string)
        return HttpResponse.json({
          id: params.conversationId,
          conversation_group_id: GROUP_ID,
          deleted_at: null,
        })
      },
    ),
  )
  return restored
}

describe('undoing a conversation delete', () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  it('restores the conversation the toast was raised for', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${MEMBER_A}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const restored = restoreRecorder()
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversation(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync(MEMBER_A))
    await act(async () => undoFromLastToast()())

    await waitFor(() => expect(restored).toEqual([MEMBER_A]))
  })

  it('fires once when Undo is double-clicked', async () => {
    // sonner leaves the button clickable for ~200ms while the toast animates out, so
    // without the guard the second click restores an already-live row and toasts a 404.
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${MEMBER_A}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const restored = restoreRecorder()
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversation(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync(MEMBER_A))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(restored).toEqual([MEMBER_A]))
  })
})

describe('undoing a conversation-group delete', () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  function mockGroupDelete() {
    server.use(
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
  }

  it('replays every member the delete cascaded to', async () => {
    // The group delete cascades and there is no group-level restore endpoint, so Undo
    // depends entirely on the member ids captured at delete time.
    mockGroupDelete()
    const restored = restoreRecorder()
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversationGroup(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ groupId: GROUP_ID, conversationIds: MEMBERS }))
    await act(async () => undoFromLastToast()())

    await waitFor(() => expect(restored).toEqual(MEMBERS))
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['conversation-group'] }))
    // `['conversation-group']` does not prefix-match `['conversation', id]`, so a bulk
    // Undo that skipped these would leave every member's detail reading as deleted.
    for (const id of MEMBERS) {
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['conversation', id] }))
    }
  })

  it('refreshes everything a conversation delete hides server-side', async () => {
    // Every one of these reads through a live conversation, so the rows vanish
    // server-side with no cascade rows involved — in both directions.
    const THROUGH_A_LIVE_CONVERSATION = [
      ['message-flags'],
      ['notes'],
      ['reviews'],
      ['review-queue'],
      ['completed-tasks'],
    ]
    mockGroupDelete()
    restoreRecorder()
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversationGroup(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ groupId: GROUP_ID, conversationIds: MEMBERS }))
    for (const queryKey of THROUGH_A_LIVE_CONVERSATION) {
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey }))
    }

    spy.mockClear()
    await act(async () => undoFromLastToast()())

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['message-flags'] })),
    )
    for (const queryKey of THROUGH_A_LIVE_CONVERSATION) {
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey }))
    }
  })

  it('reports one summary toast, not one per member', async () => {
    mockGroupDelete()
    restoreRecorder()
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversationGroup(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ groupId: GROUP_ID, conversationIds: MEMBERS }))
    await act(async () => undoFromLastToast()())

    await waitFor(() =>
      expect(vi.mocked(toast.success).mock.calls.map(([message]) => message)).toEqual([
        'Conversation deleted',
        '3 conversations restored',
      ]),
    )
  })

  it('names how many came back when one member cannot be restored', async () => {
    // A refilled group 409s the restore of the member whose slot was taken. Reporting
    // three successes and a separate error would contradict itself.
    mockGroupDelete()
    server.use(
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/:conversationId/restore`,
        ({ params }) =>
          params.conversationId === MEMBER_B
            ? HttpResponse.json(
                { title: 'Conflict', detail: 'Conversation cannot be restored.', status: 409 },
                { status: 409, headers: { 'content-type': 'application/problem+json' } },
              )
            : HttpResponse.json({
                id: params.conversationId,
                conversation_group_id: GROUP_ID,
                deleted_at: null,
              }),
      ),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteConversationGroup(EVAL_ID), { wrapper })

    await act(() => result.current.mutateAsync({ groupId: GROUP_ID, conversationIds: MEMBERS }))
    await act(async () => undoFromLastToast()())

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        'Restored 2 of 3 conversations',
        expect.objectContaining({ description: expect.stringContaining('cannot be restored') }),
      ),
    )
  })
})
