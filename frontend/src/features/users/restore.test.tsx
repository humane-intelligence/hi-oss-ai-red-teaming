import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { useDeleteUser } from './mutations'
import { UsersListPage } from './users-list-page'
import type { RoleResponse, UserResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const USER_ID = 'user-0009'

const ROLE: RoleResponse = {
  id: 'role-viewer',
  name: 'viewer',
  display_name: 'Viewer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: true,
}

const deletedUser: UserResponse = {
  id: USER_ID,
  email: 'gone@example.com',
  status: 'active',
  email_verified: true,
  roles: [ROLE],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  deleted_at: '2026-01-02T00:00:00Z',
  deleted_by_id: 'user-0001',
}

function rolesHandler() {
  return http.get('http://localhost/api/v1/roles', () =>
    HttpResponse.json({ items: [ROLE], total: 1, limit: 20, offset: 0 }),
  )
}

function usersHandler(onRequest?: (url: URL) => void, items: UserResponse[] = []) {
  return http.get('http://localhost/api/v1/auth/users', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
  })
}

function renderList(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/users']}>
          <Routes>
            <Route path="/users" element={<UsersListPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

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

describe('UsersListPage — deleted view', () => {
  it('hides the toggle from a caller without users:delete', async () => {
    server.use(usersHandler(), rolesHandler())

    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('Users')).toBeInTheDocument())
    expect(screen.queryByRole('combobox', { name: /which users to show/i })).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" sends deleted=true, newest first', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler((url) => captured.push(url)),
      rolesHandler(),
    )

    renderList(['users:read', 'users:delete'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which users to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which users to show/i, /recently deleted/i)

    await waitFor(() => {
      const deleted = captured.findLast((u) => u.searchParams.get('deleted') === 'true')
      expect(deleted).toBeDefined()
      expect(deleted?.searchParams.get('order_by')).toBe('-deleted_at')
    })
  })

  it('restores an account through the Restore action', async () => {
    const user = userEvent.setup()
    let restored: string | null = null
    server.use(
      rolesHandler(),
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        const items = deleted && !restored ? [deletedUser] : []
        return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/auth/users/${USER_ID}/restore`, () => {
        restored = USER_ID
        return HttpResponse.json({ ...deletedUser, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderList(['users:read', 'users:delete'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which users to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which users to show/i, /recently deleted/i)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /restore user/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /restore user/i }))

    await waitFor(() => expect(restored).toBe(USER_ID))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /restore user/i })).not.toBeInTheDocument(),
    )
  })

  it('offers no bulk selection over tombstones', async () => {
    const user = userEvent.setup()
    server.use(usersHandler(undefined, [deletedUser]), rolesHandler())

    renderList(['users:read', 'users:delete', 'users:update'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which users to show/i })).toBeInTheDocument(),
    )
    // Live rows are selectable for the bulk actions; a tombstone takes none of them.
    expect(screen.getAllByRole('checkbox').length).toBeGreaterThan(0)
    await chooseOption(user, /which users to show/i, /recently deleted/i)

    await waitFor(() => expect(screen.queryAllByRole('checkbox')).toHaveLength(0))
  })
})

describe('undoing a user delete', () => {
  beforeEach(() => vi.mocked(toast.success).mockClear())

  it('fires once when Undo is double-clicked', async () => {
    const attempts: string[] = []
    server.use(
      http.delete(
        `http://localhost/api/v1/auth/users/${USER_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/auth/users/${USER_ID}/restore`, () => {
        attempts.push(USER_ID)
        return HttpResponse.json({ ...deletedUser, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteUser(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toEqual([USER_ID]))
  })

  it('stales the same caches as the restore', async () => {
    // The delete tombstones the same memberships the restore brings back; a remount
    // inside staleTime must not re-render the account as live.
    server.use(
      http.delete(
        `http://localhost/api/v1/auth/users/${USER_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useDeleteUser(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))

    const keys = [
      ['users'],
      ['user', USER_ID],
      ['evaluation-group-members'],
      ['organization-members'],
    ]
    for (const queryKey of keys) {
      await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey })))
    }
    expect(spy).toHaveBeenCalledTimes(keys.length)
  })
})
