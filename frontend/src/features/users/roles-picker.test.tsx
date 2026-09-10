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

// Stands in for the endpoint's `is_object_assignable` filter, so the test exercises the
// same contract the backend implements rather than a client-side filter.
function rolesHandler(items: RoleResponse[], seen?: { query: string | null }) {
  return http.get('http://localhost/api/v1/roles', ({ request }) => {
    const param = new URL(request.url).searchParams.get('is_object_assignable')
    if (seen) seen.query = param
    const filtered =
      param === null ? items : items.filter((r) => r.is_object_assignable === (param === 'true'))
    return HttpResponse.json({ items: filtered, total: filtered.length, limit: 100, offset: 0 })
  })
}

function renderPicker(extra: { objectAssignableOnly?: boolean } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RolesPicker selected={[]} onChange={() => {}} {...extra} />
    </QueryClientProvider>,
  )
}

describe('RolesPicker', () => {
  it('shows the whole catalog by default, including custom roles (global assignment)', async () => {
    const seen = { query: null as string | null }
    server.use(rolesHandler([SYSTEM, CUSTOM], seen))
    renderPicker()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    expect(screen.getByText('Lead Reviewer')).toBeInTheDocument()
    expect(seen.query).toBeNull()
  })

  it('objectAssignableOnly asks the server for the in-group subset', async () => {
    const seen = { query: null as string | null }
    server.use(rolesHandler([SYSTEM, ADMIN, CUSTOM], seen))
    renderPicker({ objectAssignableOnly: true })

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    expect(seen.query).toBe('true')
    // admin and the global-only custom role are filtered out server-side, so the
    // component never has to know which roles those are.
    expect(screen.queryByText('Admin')).toBeNull()
    expect(screen.queryByText('Lead Reviewer')).toBeNull()
  })

  it('renders an empty-state when the filtered catalog comes back empty', async () => {
    server.use(rolesHandler([ADMIN]))
    renderPicker({ objectAssignableOnly: true })

    await waitFor(() => expect(screen.getByText(/no roles available to pick/i)).toBeInTheDocument())
  })
})
