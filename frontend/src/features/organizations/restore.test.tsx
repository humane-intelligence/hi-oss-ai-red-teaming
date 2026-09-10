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
import { useDeleteOrganization } from './mutations'
import { OrganizationsListPage } from './organizations-list-page'
import type { OrganizationResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const ORG_ID = 'org-0001-0000-0000-000000000000'

const deletedOrg: OrganizationResponse = {
  id: ORG_ID,
  name: 'Acme Corp',
  description: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  deleted_at: '2026-01-02T00:00:00Z',
  deleted_by_id: 'user-0001',
}

function orgsHandler(onRequest?: (url: URL) => void, items: OrganizationResponse[] = []) {
  return http.get('http://localhost/api/v1/organizations', ({ request }) => {
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
          <OrganizationsListPage />
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

describe('OrganizationsListPage — deleted view', () => {
  it('hides the toggle from a caller without organizations:delete', async () => {
    server.use(orgsHandler())

    renderPage(['organizations:read'])

    await waitFor(() => expect(screen.getByText('Organizations')).toBeInTheDocument())
    expect(
      screen.queryByRole('combobox', { name: /which organizations to show/i }),
    ).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" sends deleted=true, newest first', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(orgsHandler((url) => captured.push(url)))

    renderPage(['organizations:read', 'organizations:delete'])

    await waitFor(() =>
      expect(
        screen.getByRole('combobox', { name: /which organizations to show/i }),
      ).toBeInTheDocument(),
    )
    await chooseOption(user, /which organizations to show/i, /recently deleted/i)

    await waitFor(() => {
      const deleted = captured.findLast((u) => u.searchParams.get('deleted') === 'true')
      expect(deleted).toBeDefined()
      expect(deleted?.searchParams.get('order_by')).toBe('-deleted_at')
    })
  })

  it('restores a row through the Restore action', async () => {
    const user = userEvent.setup()
    let restored: string | null = null
    server.use(
      http.get('http://localhost/api/v1/organizations', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        const items = deleted && !restored ? [deletedOrg] : []
        return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/organizations/${ORG_ID}/restore`, () => {
        restored = ORG_ID
        return HttpResponse.json({ ...deletedOrg, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderPage(['organizations:read', 'organizations:delete'])

    await waitFor(() =>
      expect(
        screen.getByRole('combobox', { name: /which organizations to show/i }),
      ).toBeInTheDocument(),
    )
    await chooseOption(user, /which organizations to show/i, /recently deleted/i)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /restore organization/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /restore organization/i }))

    await waitFor(() => expect(restored).toBe(ORG_ID))
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /restore organization/i }),
      ).not.toBeInTheDocument(),
    )
  })
})

describe('undoing an organization delete', () => {
  beforeEach(() => vi.mocked(toast.success).mockClear())

  it('fires once when Undo is double-clicked', async () => {
    const attempts: string[] = []
    server.use(
      http.delete(
        `http://localhost/api/v1/organizations/${ORG_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/organizations/${ORG_ID}/restore`, () => {
        attempts.push(ORG_ID)
        return HttpResponse.json({ ...deletedOrg, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useDeleteOrganization(), { wrapper })

    await act(() => result.current.mutateAsync(ORG_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toEqual([ORG_ID]))
  })

  it('refreshes the member-facing caches, not just the org list', async () => {
    // The delete leaves members pointing at the tombstone, so their embedded org goes
    // blank — the users caches are stale in both directions.
    server.use(
      http.delete(
        `http://localhost/api/v1/organizations/${ORG_ID}`,
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
    const { result } = renderHook(() => useDeleteOrganization(), { wrapper })

    await act(() => result.current.mutateAsync(ORG_ID))

    const keys = [
      ['organizations'],
      ['organization', ORG_ID],
      ['organization-lookup'],
      ['organization-members', ORG_ID],
      ['users'],
      ['user'],
    ]
    for (const queryKey of keys) {
      await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey })))
    }
    expect(spy).toHaveBeenCalledTimes(keys.length)
  })
})
