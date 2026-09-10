import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { RolesListPage } from './roles-list-page'
import type { RoleResponse } from '@/lib/api/types'

const ROLE_ADMIN: RoleResponse = {
  id: 'r-admin',
  name: 'admin',
  display_name: 'Admin',
  permissions: ['users:read'],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}
const ROLE_CUSTOM: RoleResponse = {
  id: 'r-lead',
  name: 'lead_reviewer',
  display_name: 'Lead Reviewer',
  permissions: ['reviews:read'],
  is_system: false,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: true,
}
const ROLE_INACTIVE: RoleResponse = {
  id: 'r-old',
  name: 'old_role',
  display_name: 'Old Role',
  permissions: [],
  is_system: false,
  is_active: false,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}

function rolesHandler(items: RoleResponse[], onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/roles', ({ request }) => {
    const url = new URL(request.url)
    onRequest?.(url)
    const visible =
      url.searchParams.get('include_inactive') === 'true' ? items : items.filter((r) => r.is_active)
    return HttpResponse.json({ items: visible, total: visible.length, limit: 20, offset: 0 })
  })
}

function renderList(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/roles']}>
          <Routes>
            <Route path="/roles" element={<RolesListPage />} />
            <Route path="/roles/new" element={<div>New role page</div>} />
            <Route path="/roles/:id/edit" element={<div>Edit role page</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('RolesListPage', () => {
  it('lists roles', async () => {
    server.use(rolesHandler([ROLE_ADMIN, ROLE_CUSTOM]))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    expect(screen.getByText('Admin')).toBeInTheDocument()
  })

  it('badges a custom role that is assignable inside groups', async () => {
    server.use(rolesHandler([ROLE_ADMIN, ROLE_CUSTOM]))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    // Only the opted-in custom role carries it; system-role policy is code-managed.
    expect(screen.getAllByText('In-group')).toHaveLength(1)
  })

  it('lists the permissions as a title hint on the count', async () => {
    const MULTI = { ...ROLE_CUSTOM, permissions: ['reviews:read', 'reviews:annotate'] }
    server.use(rolesHandler([MULTI]))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    expect(screen.getByText('2')).toHaveAttribute('title', 'reviews:read\nreviews:annotate')
  })

  it('shows New role for a manager', async () => {
    server.use(rolesHandler([ROLE_CUSTOM]))
    renderList(['roles:read', 'roles:manage'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /new role/i })).toBeInTheDocument()
  })

  it('hides New role for a read-only caller', async () => {
    server.use(rolesHandler([ROLE_CUSTOM]))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /new role/i })).toBeNull()
  })

  it('row click opens the edit page for a manager', async () => {
    const user = userEvent.setup()
    server.use(rolesHandler([ROLE_CUSTOM]))
    renderList(['roles:read', 'roles:manage'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    await user.click(screen.getByText('Lead Reviewer'))
    await waitFor(() => expect(screen.getByText('Edit role page')).toBeInTheDocument())
  })

  it('does not navigate on row click for a read-only caller', async () => {
    const user = userEvent.setup()
    server.use(rolesHandler([ROLE_CUSTOM]))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    await user.click(screen.getByText('Lead Reviewer'))
    expect(screen.queryByText('Edit role page')).toBeNull()
  })

  it('Show inactive reveals inactive roles and requests include_inactive=true', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(rolesHandler([ROLE_CUSTOM, ROLE_INACTIVE], (u) => captured.push(u)))
    renderList(['roles:read'])

    await waitFor(() => expect(screen.getByText('Lead Reviewer')).toBeInTheDocument())
    expect(screen.queryByText('Old Role')).toBeNull()

    await user.click(screen.getByRole('checkbox', { name: /show inactive/i }))

    await waitFor(() => expect(screen.getByText('Old Role')).toBeInTheDocument())
    expect(captured.some((u) => u.searchParams.get('include_inactive') === 'true')).toBe(true)
  })
})
