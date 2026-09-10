import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { useRemoveGroupMember } from './mutations'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const USER_ID = 'usr-0001-0000-0000-000000000000'
const ROLE_ID = 'rol-0001-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

// The Undo action lives inside the toast options, and no <Toaster> is mounted in tests.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

function mockRemove() {
  server.use(
    http.delete(
      `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/${USER_ID}`,
      () => new HttpResponse(null, { status: 204 }),
    ),
  )
}

describe('undoing a member removal', () => {
  beforeEach(() => vi.mocked(toast.success).mockClear())

  it('re-adds the member with exactly the roles the row held', async () => {
    // An assignment row carries no state beyond (group, user, role), so the undo is a
    // re-add — which also re-validates the roles instead of reviving them blindly.
    mockRemove()
    const bodies: unknown[] = []
    server.use(
      http.post(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`,
        async ({ request }) => {
          bodies.push(await request.json())
          return HttpResponse.json({ user: { id: USER_ID }, roles: [] })
        },
      ),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useRemoveGroupMember(GROUP_ID), { wrapper })

    await act(() => result.current.mutateAsync({ userId: USER_ID, roleIds: [ROLE_ID] }))
    await act(async () => undoFromLastToast()())

    await waitFor(() => expect(bodies).toEqual([{ user_id: USER_ID, role_ids: [ROLE_ID] }]))
  })

  it('fires once when Undo is double-clicked', async () => {
    mockRemove()
    let attempts = 0
    server.use(
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () => {
        attempts += 1
        return HttpResponse.json({ user: { id: USER_ID }, roles: [] })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useRemoveGroupMember(GROUP_ID), { wrapper })

    await act(() => result.current.mutateAsync({ userId: USER_ID, roleIds: [ROLE_ID] }))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toBe(1))
  })
})
