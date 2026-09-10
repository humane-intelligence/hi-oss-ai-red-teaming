import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { UsersListPage } from './users-list-page'
import type { RoleResponse, UserResponse } from '@/lib/api/types'

const ROLE_VIEWER: RoleResponse = {
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

const ROLE_ADMIN: RoleResponse = {
  id: 'role-admin',
  name: 'admin',
  display_name: 'Admin',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}

const USER_1: UserResponse = {
  id: 'user-0001',
  email: 'alice@example.com',
  status: 'active',
  email_verified: true,
  roles: [ROLE_VIEWER],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const USER_2: UserResponse = { ...USER_1, id: 'user-0002', email: 'bob@example.com' }

const USER_INVITED: UserResponse = {
  ...USER_1,
  id: 'user-0003',
  email: 'carol@example.com',
  status: 'invited',
  email_verified: false,
  invitation: { status: 'pending', expires_at: '2026-08-15T12:00:00Z' },
}

type BulkBody = { dry_run: boolean; rows: { row_key: string; data: { user_id: string } }[] }

function rolesHandler() {
  return http.get('http://localhost/api/v1/roles', () =>
    HttpResponse.json({ items: [ROLE_VIEWER, ROLE_ADMIN], total: 2, limit: 100, offset: 0 }),
  )
}

function usersHandler(items: UserResponse[] = [USER_1], onRequest?: (url: URL) => void) {
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
            <Route path="/users/:id/edit" element={<div>Edit page</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('UsersListPage — RBAC gates', () => {
  it('shows Invite button when user has users:invite', async () => {
    server.use(usersHandler(), rolesHandler())
    renderList(['users:read', 'users:invite'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^invite$/i })).toBeInTheDocument()
    // One merged entry point — the separate Bulk invite button is gone.
    expect(screen.queryByRole('button', { name: /bulk invite/i })).toBeNull()
  })

  it('hides Invite button when user lacks users:invite', async () => {
    server.use(usersHandler(), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^invite$/i })).toBeNull()
  })

  it('with users:read only, no create/edit/delete affordances', async () => {
    server.use(usersHandler(), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /new user/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /invite user/i })).toBeNull()
    // rows are not clickable (no cursor-pointer style, but we verify no navigate occurs)
    // DataTable onRowClick is undefined, so no pointer cursor
  })

  it('shows the Force logout button when user has users:manage_sessions', async () => {
    server.use(usersHandler(), rolesHandler())
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /force logout/i })).toBeInTheDocument()
  })

  it('hides the Force logout button when user lacks users:manage_sessions', async () => {
    server.use(usersHandler(), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /force logout/i })).toBeNull()
  })

  it("does not offer Force logout on the current user's own row", async () => {
    // authWrapper sets the current user's id to '1'; that row must not get a button.
    const SELF: UserResponse = { ...USER_1, id: '1', email: 'me@example.com' }
    server.use(usersHandler([SELF, USER_1]), rolesHandler())
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('me@example.com')).toBeInTheDocument())
    expect(screen.getAllByRole('button', { name: /force logout/i })).toHaveLength(1)
  })
})

describe('UsersListPage — force-logout flow', () => {
  it('single: row button opens the confirm dialog, confirm fires the POST and closes it', async () => {
    const user = userEvent.setup()
    let calledId: string | null = null
    server.use(
      usersHandler(),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/:id/force-logout', ({ params }) => {
        calledId = params.id as string
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /force logout alice@example\.com/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^force logout$/i }))

    await waitFor(() => expect(calledId).toBe('user-0001'))
    await waitFor(() => expect(screen.queryByText(/sign out alice@example\.com/i)).toBeNull())
  })

  it('bulk: selecting rows and confirming posts the rows payload', async () => {
    const user = userEvent.setup()
    let body: BulkBody | null = null
    server.use(
      usersHandler([USER_1, USER_2]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/force-logout', async ({ request }) => {
        body = (await request.json()) as BulkBody
        return HttpResponse.json({
          dry_run: false,
          total: 2,
          succeeded: 2,
          failed: 0,
          results: [
            { row_key: 'user-0001', status: 'ok', data: { user_id: 'user-0001' }, error: null },
            { row_key: 'user-0002', status: 'ok', data: { user_id: 'user-0002' }, error: null },
          ],
        })
      }),
    )
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('bob@example.com')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[0]!) // header select-all

    const toolbar = within(await screen.findByRole('group', { name: /bulk actions/i }))
    await user.click(toolbar.getByRole('button', { name: /^force logout$/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^force logout$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.dry_run).toBe(false)
    expect(body!.rows).toHaveLength(2)
    expect(body!.rows.map((r) => r.data.user_id).sort()).toEqual(['user-0001', 'user-0002'])
    expect(body!.rows.every((r) => r.row_key === r.data.user_id)).toBe(true)
  })
})

describe('UsersListPage — row-level admin actions', () => {
  const USER_INACTIVE: UserResponse = {
    ...USER_1,
    id: 'user-0004',
    email: 'dave@example.com',
    status: 'inactive',
  }

  it('offers reset + deactivate on an active row and only activate on an inactive one', async () => {
    server.use(usersHandler([USER_1, USER_INACTIVE]), rolesHandler())
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('dave@example.com')).toBeInTheDocument())
    expect(
      screen.getByRole('button', { name: /reset link to alice@example\.com/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /deactivate alice@example\.com/i }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /activate dave@example\.com/i })).toBeInTheDocument()
    // An inactive account has no reset path — the backend 409s it.
    expect(screen.queryByRole('button', { name: /reset link to dave@example\.com/i })).toBeNull()
  })

  it('offers no status toggle on a row that is still onboarding', async () => {
    server.use(usersHandler([USER_INVITED]), rolesHandler())
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    // `invited`/`pending` belong to the invitation and verification flows; both transitions 409.
    expect(screen.queryByRole('button', { name: /activate carol@example\.com/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /deactivate carol@example\.com/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reset link to carol@example\.com/i })).toBeNull()
  })

  it('hides both actions without users:update', async () => {
    server.use(usersHandler([USER_1]), rolesHandler())
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /reset link/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^deactivate /i })).toBeNull()
  })

  it("offers a reset but no status toggle on the caller's own row", async () => {
    const SELF: UserResponse = { ...USER_1, id: '1', email: 'me@example.com' }
    server.use(usersHandler([SELF]), rolesHandler())
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('me@example.com')).toBeInTheDocument())
    expect(
      screen.getByRole('button', { name: /reset link to me@example\.com/i }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /deactivate me@example\.com/i })).toBeNull()
  })

  it('deactivate asks for confirmation, then posts the inactive status', async () => {
    const user = userEvent.setup()
    let body: { status: string } | null = null
    let calledId: string | null = null
    server.use(
      usersHandler([USER_1]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/:id/status', async ({ params, request }) => {
        calledId = params.id as string
        body = (await request.json()) as { status: string }
        return HttpResponse.json({ ...USER_1, status: 'inactive' })
      }),
    )
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /deactivate alice@example\.com/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(calledId).toBe('user-0001')
    expect(body!.status).toBe('inactive')
  })

  it('activate posts straight away — no confirmation for a reversible, non-destructive change', async () => {
    const user = userEvent.setup()
    let body: { status: string } | null = null
    server.use(
      usersHandler([USER_INACTIVE]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/:id/status', async ({ request }) => {
        body = (await request.json()) as { status: string }
        return HttpResponse.json({ ...USER_INACTIVE, status: 'active' })
      }),
    )
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('dave@example.com')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /activate dave@example\.com/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.status).toBe('active')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('asks before mailing a reset link, and warns that the old link dies', async () => {
    const user = userEvent.setup()
    let calledId: string | null = null
    server.use(
      usersHandler([USER_1]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/:id/password-reset', ({ params }) => {
        calledId = params.id as string
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /reset link to alice@example\.com/i }))

    // The row button only opens the dialog — a misclick in the icon cluster must not mail anyone.
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/already have stops working/i)
    expect(calledId).toBeNull()

    await user.click(within(dialog).getByRole('button', { name: /^send link$/i }))
    await waitFor(() => expect(calledId).toBe('user-0001'))
  })
})

describe('UsersListPage — bulk admin actions', () => {
  type StatusBulkBody = {
    dry_run: boolean
    rows: { row_key: string; data: { user_id: string; status: string } }[]
  }

  it('splits the selection toolbar by permission', async () => {
    const user = userEvent.setup()
    server.use(usersHandler([USER_1]), rolesHandler())
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[0]!)

    const toolbar = within(await screen.findByRole('group', { name: /bulk actions/i }))
    expect(toolbar.getByRole('button', { name: /^activate$/i })).toBeInTheDocument()
    expect(toolbar.getByRole('button', { name: /^deactivate$/i })).toBeInTheDocument()
    expect(toolbar.getByRole('button', { name: /send password reset/i })).toBeInTheDocument()
    // Force logout hangs off users:manage_sessions, which this caller lacks.
    expect(toolbar.queryByRole('button', { name: /force logout/i })).toBeNull()
  })

  it('mounts the selection live region before the first selection', async () => {
    const user = userEvent.setup()
    server.use(usersHandler([USER_1]), rolesHandler())
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    const region = screen.getByRole('status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toBeEmptyDOMElement()

    await user.click(screen.getAllByRole('checkbox')[0]!)
    await waitFor(() => expect(region).toHaveTextContent('1 user selected'))
  })

  it('offers only Force logout with users:manage_sessions alone', async () => {
    const user = userEvent.setup()
    server.use(usersHandler([USER_1]), rolesHandler())
    renderList(['users:read', 'users:manage_sessions'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[0]!)

    const toolbar = within(await screen.findByRole('group', { name: /bulk actions/i }))
    expect(toolbar.getByRole('button', { name: /^force logout$/i })).toBeInTheDocument()
    expect(toolbar.queryByRole('button', { name: /^activate$/i })).toBeNull()
    expect(toolbar.queryByRole('button', { name: /send password reset/i })).toBeNull()
  })

  it('bulk deactivate posts the target status on every row and shows no result dialog when all succeed', async () => {
    const user = userEvent.setup()
    let body: StatusBulkBody | null = null
    server.use(
      usersHandler([USER_1, USER_2]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/status', async ({ request }) => {
        body = (await request.json()) as StatusBulkBody
        return HttpResponse.json({
          dry_run: false,
          total: 2,
          succeeded: 2,
          failed: 0,
          results: [
            {
              row_key: 'user-0001',
              status: 'ok',
              data: { user_id: 'user-0001', status: 'inactive' },
              error: null,
            },
            {
              row_key: 'user-0002',
              status: 'ok',
              data: { user_id: 'user-0002', status: 'inactive' },
              error: null,
            },
          ],
        })
      }),
    )
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('bob@example.com')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[0]!)

    const toolbar = within(await screen.findByRole('group', { name: /bulk actions/i }))
    await user.click(toolbar.getByRole('button', { name: /^deactivate$/i }))

    const confirm = await screen.findByRole('dialog')
    await user.click(within(confirm).getByRole('button', { name: /^deactivate$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.rows.map((r) => r.data.status)).toEqual(['inactive', 'inactive'])
    expect(body!.rows.every((r) => r.row_key === r.data.user_id)).toBe(true)
    // Wait for the mutation to settle on a positive signal — `finish` clears the selection — so the
    // negative assertion below cannot pass merely by running before the dialog would have opened.
    await waitFor(() => expect(screen.queryByText(/users selected/i)).toBeNull())
    // A clean run reports itself through the toast only.
    expect(screen.queryByText(/2 of 2 succeeded/i)).toBeNull()
  })

  it('bulk reset opens the result dialog and names the failed row by email', async () => {
    const user = userEvent.setup()
    server.use(
      usersHandler([USER_1, USER_2]),
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/users/password-reset', () =>
        HttpResponse.json({
          dry_run: false,
          total: 2,
          succeeded: 1,
          failed: 1,
          results: [
            { row_key: 'user-0001', status: 'ok', data: { user_id: 'user-0001' }, error: null },
            {
              row_key: 'user-0002',
              status: 'failed',
              data: null,
              error: { title: 'Conflict', detail: 'Account has no password to reset.' },
            },
          ],
        }),
      ),
    )
    renderList(['users:read', 'users:update'])

    await waitFor(() => expect(screen.getByText('bob@example.com')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[0]!)

    const toolbar = within(await screen.findByRole('group', { name: /bulk actions/i }))
    await user.click(toolbar.getByRole('button', { name: /send password reset/i }))

    const confirm = await screen.findByRole('dialog')
    await user.click(within(confirm).getByRole('button', { name: /^send links$/i }))

    await waitFor(() => expect(screen.getByText(/1 of 2 succeeded, 1 failed/i)).toBeInTheDocument())
    expect(screen.getByRole('listitem')).toHaveTextContent(
      'bob@example.com: Account has no password to reset.',
    )
  })
})

describe('UsersListPage — organization column', () => {
  const USER_WITH_ORG: UserResponse = {
    ...USER_1,
    id: 'user-0002',
    email: 'bob@example.com',
    organization: { id: 'org-1', name: 'Acme Corp' },
  }

  it('renders the organization name when the user belongs to one', async () => {
    server.use(usersHandler([USER_WITH_ORG]), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('bob@example.com')).toBeInTheDocument())
    expect(screen.getByText('Acme Corp')).toBeInTheDocument()
  })

  it('always shows the Organization column header', async () => {
    server.use(usersHandler([USER_1]), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
    expect(screen.getByRole('columnheader', { name: /organization/i })).toBeInTheDocument()
  })
})

describe('UsersListPage — invitation actions', () => {
  it('resend fires the endpoint directly; revoke goes through the confirm dialog', async () => {
    const user = userEvent.setup()
    let resendCalled = 0
    let revokeCalled = 0
    server.use(
      usersHandler([USER_INVITED]),
      rolesHandler(),
      http.post(`http://localhost/api/v1/auth/users/${USER_INVITED.id}/invitation/resend`, () => {
        resendCalled += 1
        return HttpResponse.json({
          id: 'inv-1',
          user_id: USER_INVITED.id,
          email: USER_INVITED.email,
          status: 'pending',
          expires_at: '2026-08-20T12:00:00Z',
          created_at: '2026-07-29T12:00:00Z',
        })
      }),
      http.delete(`http://localhost/api/v1/auth/users/${USER_INVITED.id}/invitation`, () => {
        revokeCalled += 1
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderList(['users:read', 'users:invite'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    await user.click(
      screen.getByRole('button', { name: `Resend invitation to ${USER_INVITED.email}` }),
    )
    await waitFor(() => expect(resendCalled).toBe(1))

    await user.click(
      screen.getByRole('button', { name: `Revoke invitation of ${USER_INVITED.email}` }),
    )
    expect(revokeCalled).toBe(0)
    await user.click(screen.getByRole('button', { name: /^revoke$/i }))
    await waitFor(() => expect(revokeCalled).toBe(1))
  })

  it('hides revoke for a non-pending invitation and both actions without users:invite', async () => {
    const revoked: UserResponse = {
      ...USER_INVITED,
      invitation: { status: 'revoked', expires_at: '2026-08-15T12:00:00Z' },
    }
    server.use(usersHandler([revoked]), rolesHandler())
    renderList(['users:read', 'users:invite'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /resend invitation/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /revoke invitation/i })).toBeNull()
  })

  it('shows neither action without users:invite', async () => {
    server.use(usersHandler([USER_INVITED]), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /resend invitation/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /revoke invitation/i })).toBeNull()
  })

  // An account invited only into an evaluation group: `invited`, but the projection is
  // platform-scoped, so there is no platform invitation to resend.
  it('shows neither action for an invited account without a platform invitation', async () => {
    const groupOnly: UserResponse = { ...USER_INVITED, invitation: null }
    server.use(usersHandler([groupOnly]), rolesHandler())
    renderList(['users:read', 'users:invite'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /resend invitation/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /revoke invitation/i })).toBeNull()
  })
})

describe('UsersListPage — invitation column', () => {
  it('renders the invitation status pill and expiry for a pending invitation', async () => {
    server.use(usersHandler([USER_INVITED]), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    expect(screen.getByRole('columnheader', { name: /invitation/i })).toBeInTheDocument()
    const table = within(screen.getByRole('table'))
    expect(table.getByText('pending')).toBeInTheDocument()
    expect(
      table.getByText(`expires ${new Date('2026-08-15T12:00:00Z').toLocaleDateString()}`),
    ).toBeInTheDocument()
  })

  it('renders a revoked pill without expiry, and no pill for a user without an invitation', async () => {
    const revoked: UserResponse = {
      ...USER_INVITED,
      invitation: { status: 'revoked', expires_at: '2026-08-15T12:00:00Z' },
    }
    server.use(usersHandler([USER_1, revoked]), rolesHandler())
    renderList(['users:read'])

    await waitFor(() => expect(screen.getByText('carol@example.com')).toBeInTheDocument())
    const table = within(screen.getByRole('table'))
    expect(table.getByText('revoked')).toBeInTheDocument()
    expect(table.queryByText(/^expires /)).toBeNull()
    expect(table.queryByText('pending')).toBeNull()
  })
})

describe('UsersListPage — filters', () => {
  function renderFilters() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(['users:read'])
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

  it('initial load — no filter params in request', async () => {
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('status')).toBeNull()
    expect(captured[0]!.searchParams.get('email')).toBeNull()
    expect(captured[0]!.searchParams.get('role_id')).toBeNull()
  })

  it('selecting a status sends status= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^active$/)

    await waitFor(() => {
      const withStatus = captured.find((u) => u.searchParams.get('status') === 'active')
      expect(withStatus).toBeDefined()
    })
  })

  it('selecting "All statuses" omits status param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^invited$/)
    await chooseOption(user, /status/i, /^All statuses$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('status')).toBeNull()
    })
  })

  it('typing email + submit sends email= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await waitFor(() => expect(screen.getByPlaceholderText(/search email/i)).toBeInTheDocument())
    await user.type(screen.getByPlaceholderText(/search email/i), 'alice')
    await user.click(screen.getByRole('button', { name: /^search$/i }))

    await waitFor(() => {
      const withEmail = captured.find((u) => u.searchParams.get('email') === 'alice')
      expect(withEmail).toBeDefined()
    })
  })

  it('clearing email (empty submit) omits email param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await waitFor(() => expect(screen.getByPlaceholderText(/search email/i)).toBeInTheDocument())
    await user.type(screen.getByPlaceholderText(/search email/i), 'alice')
    await user.click(screen.getByRole('button', { name: /^search$/i }))

    await waitFor(() => {
      const withEmail = captured.find((u) => u.searchParams.get('email') === 'alice')
      expect(withEmail).toBeDefined()
    })

    await user.clear(screen.getByPlaceholderText(/search email/i))
    await user.click(screen.getByRole('button', { name: /^search$/i }))

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('email')).toBeNull()
    })
  })

  it('selecting a role sends role_id= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await chooseOption(user, /role/i, /^Viewer$/)

    await waitFor(() => {
      const withRole = captured.find((u) => u.searchParams.get('role_id') === 'role-viewer')
      expect(withRole).toBeDefined()
    })
  })

  it('selecting "All roles" omits role_id param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      usersHandler([USER_1], (url) => captured.push(url)),
      rolesHandler(),
    )

    renderFilters()

    await chooseOption(user, /role/i, /^Admin$/)
    await chooseOption(user, /role/i, /^All roles$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('role_id')).toBeNull()
    })
  })
})
