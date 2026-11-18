import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { RolesPicker } from './roles-picker'
import type { RoleResponse } from '@/lib/api/types'

const SYSTEM: RoleResponse = {
  id: 's',
  name: 'red_teamer',
  display_name: 'Red Teamer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: true,
}
const ADMIN: RoleResponse = {
  ...SYSTEM,
  id: 'a',
  name: 'admin',
  display_name: 'Admin',
  is_object_assignable: false,
}
const CUSTOM: RoleResponse = {
  ...SYSTEM,
  id: 'c',
  name: 'lead',
  display_name: 'Lead Reviewer',
  is_system: false,
  is_object_assignable: false,
}

function rolesHandler(items: RoleResponse[], seen?: { called: boolean }) {
  return http.get('http://localhost/api/v1/roles', () => {
    if (seen) seen.called = true
    return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
  })
}

function renderPicker(extra: { groupId?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RolesPicker selected={[]} onChange={() => {}} {...extra} />
    </QueryClientProvider>,
  )
}

describe('RolesPicker', () => {
  it('shows the whole catalog by default, including custom roles (global assignment)', async () => {
    server.use(rolesHandler([SYSTEM, CUSTOM]))
    renderPicker()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    expect(screen.getByText('Lead Reviewer')).toBeInTheDocument()
  })

  it('with a groupId offers the roles assignable in that group, without reading the catalog', async () => {
    const seen = { called: false }
    server.use(
      rolesHandler([SYSTEM, ADMIN, CUSTOM], seen),
      http.get('http://localhost/api/v1/evaluation-groups/grp-1/assignable-roles', () =>
        HttpResponse.json({ items: [SYSTEM], total: 1, limit: 100, offset: 0 }),
      ),
    )
    renderPicker({ groupId: 'grp-1' })

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    expect(screen.queryByText('Admin')).toBeNull()
    expect(seen.called).toBe(false)
  })

  it('renders an empty-state when the group offers no roles', async () => {
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups/grp-1/assignable-roles', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderPicker({ groupId: 'grp-1' })

    await waitFor(() => expect(screen.getByText(/no roles available to pick/i)).toBeInTheDocument())
  })
})
