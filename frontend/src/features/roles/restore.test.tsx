import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { useDeleteRole } from './mutations'
import { RolesListPage } from './roles-list-page'
import type { RoleResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const ROLE_ID = 'role-0001-0000-0000-000000000000'

const deletedRole: RoleResponse = {
  id: ROLE_ID,
  name: 'auditor',
  display_name: 'Auditor',
  permissions: [],
  is_system: false,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
  deleted_at: '2026-01-02T00:00:00Z',
  deleted_by_id: 'user-0001',
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

// The Undo action lives inside the toast options, and no <Toaster> is mounted in tests,
// so the only way to exercise it is to pull the handler off the mocked toast call.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

function rolesHandler(onRequest?: (url: URL) => void, items: RoleResponse[] = []) {
  return http.get('http://localhost/api/v1/roles', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
  })
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RolesListPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('RolesListPage — deleted view', () => {
  it('hides the toggle from a caller without roles:manage', async () => {
    server.use(rolesHandler())

    renderPage(['roles:read'])

    await waitFor(() => expect(screen.getByText('Roles')).toBeInTheDocument())
    expect(screen.queryByRole('combobox', { name: /which roles to show/i })).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" asks for tombstones, inactive ones included', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(rolesHandler((url) => captured.push(url)))

    renderPage(['roles:read', 'roles:manage'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which roles to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which roles to show/i, /recently deleted/i)

    await waitFor(() => {
      const deleted = captured.findLast((u) => u.searchParams.get('deleted') === 'true')
      expect(deleted).toBeDefined()
      // `include_inactive` narrows both branches alike, so the tombstone view has to ask for
      // everything or a deactivated role's tombstone is unreachable.
      expect(deleted?.searchParams.get('include_inactive')).toBe('true')
    })
  })

  it('restores a row through the Restore action', async () => {
    const user = userEvent.setup()
    let restored: string | null = null
    server.use(
      http.get('http://localhost/api/v1/roles', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        const items = deleted && !restored ? [deletedRole] : []
        return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/roles/${ROLE_ID}/restore`, () => {
        restored = ROLE_ID
        return HttpResponse.json({ ...deletedRole, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderPage(['roles:read', 'roles:manage'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which roles to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which roles to show/i, /recently deleted/i)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /restore/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /restore/i }))

    await waitFor(() => expect(restored).toBe(ROLE_ID))
    // Not just the request: the row has to leave the deleted list, which only happens if
    // the mutation invalidates the listing.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /restore/i })).not.toBeInTheDocument(),
    )
  })
})

describe('undoing a role delete', () => {
  beforeEach(() => vi.mocked(toast.success).mockClear())

  it('fires once when Undo is double-clicked', async () => {
    const attempts: string[] = []
    server.use(
      http.delete(
        `http://localhost/api/v1/roles/${ROLE_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/roles/${ROLE_ID}/restore`, () => {
        attempts.push(ROLE_ID)
        return HttpResponse.json({ ...deletedRole, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteRole(), { wrapper })

    await act(() => result.current.mutateAsync(ROLE_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toEqual([ROLE_ID]))
  })

  it('re-arms Undo after a failed restore', async () => {
    // The latch flips before the mutation, so a failed restore must flip it back —
    // otherwise the toast keeps an enabled Undo that silently no-ops.
    const attempts: string[] = []
    server.use(
      http.delete(
        `http://localhost/api/v1/roles/${ROLE_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/roles/${ROLE_ID}/restore`, () => {
        attempts.push(ROLE_ID)
        return attempts.length === 1
          ? HttpResponse.json({ detail: 'boom' }, { status: 500 })
          : HttpResponse.json({ ...deletedRole, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteRole(), { wrapper })

    await act(() => result.current.mutateAsync(ROLE_ID))
    const undo = undoFromLastToast()
    await act(async () => undo())
    await waitFor(() => expect(attempts).toEqual([ROLE_ID]))

    await act(async () => undo())

    await waitFor(() => expect(attempts).toEqual([ROLE_ID, ROLE_ID]))
  })
})
