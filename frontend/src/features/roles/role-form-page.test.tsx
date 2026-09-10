import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { RoleFormPage } from './role-form-page'
import type { RoleResponse } from '@/lib/api/types'

const PERMISSIONS = [
  { key: 'reviews:read', description: 'Read reviews.', is_delegable: true },
  { key: 'reviews:annotate', description: 'Annotate submissions.', is_delegable: true },
  { key: 'roles:manage', description: 'Manage roles.', is_delegable: false },
  { key: 'users:manage_admin', description: 'Manage admin users.', is_delegable: false },
  { key: 'evaluation_groups:read', description: 'Read evaluation groups.', is_delegable: true },
]

const ROLE_CUSTOM: RoleResponse = {
  id: 'r-lead',
  name: 'lead_reviewer',
  display_name: 'Lead Reviewer',
  description: 'Leads reviews.',
  permissions: ['reviews:read'],
  is_system: false,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}
// Carries the in-group read permission the assignability toggle requires.
const ROLE_IN_GROUP: RoleResponse = {
  ...ROLE_CUSTOM,
  id: 'r-ig',
  name: 'in_group',
  display_name: 'In Group',
  permissions: ['evaluation_groups:read'],
}
// Already group-grantable — the precondition for becoming the self-join default.
const ROLE_GRANTABLE: RoleResponse = { ...ROLE_IN_GROUP, id: 'r-gr', is_object_assignable: true }
const ROLE_ADMIN: RoleResponse = {
  id: 'r-admin',
  name: 'admin',
  display_name: 'Admin',
  description: 'Administrator.',
  permissions: ['users:read'],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}
// `admin`/`owner` can never be deactivated (NON_DEACTIVATABLE_SYSTEM_ROLES); `viewer` can.
const ROLE_VIEWER: RoleResponse = {
  ...ROLE_ADMIN,
  id: 'r-viewer',
  name: 'viewer',
  display_name: 'Viewer',
  is_object_assignable: true,
}

function roleHandler(role: RoleResponse) {
  return http.get('http://localhost/api/v1/roles/:role_id', () => HttpResponse.json(role))
}
function permissionsHandler() {
  return http.get('http://localhost/api/v1/permissions', () =>
    HttpResponse.json({ items: PERMISSIONS, total: PERMISSIONS.length, limit: 200, offset: 0 }),
  )
}

function renderForm(route: string, permissions = ['roles:read', 'roles:manage']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/roles" element={<div>Roles list</div>} />
            <Route path="/roles/new" element={<RoleFormPage />} />
            <Route path="/roles/:id/edit" element={<RoleFormPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('RoleFormPage — create', () => {
  it('creates a custom role with the chosen permissions', async () => {
    const user = userEvent.setup()
    let body: { name: string; display_name: string; permissions: string[] } | null = null
    server.use(
      permissionsHandler(),
      http.post('http://localhost/api/v1/roles', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({
          id: 'r-new',
          name: body!.name,
          display_name: body!.display_name,
          permissions: body!.permissions,
          is_system: false,
          is_active: true,
          is_default: false,
          is_participant_default: false,
        })
      }),
    )
    renderForm('/roles/new')

    await waitFor(() => expect(screen.getByText('reviews:read')).toBeInTheDocument())
    await user.type(screen.getByLabelText('Name'), 'lead_reviewer')
    await user.type(screen.getByLabelText('Display name'), 'Lead Reviewer')
    await user.click(screen.getByRole('checkbox', { name: /reviews:read/i }))
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.name).toBe('lead_reviewer')
    expect(body!.display_name).toBe('Lead Reviewer')
    expect(body!.permissions).toEqual(['reviews:read'])
  })

  it('rejects an invalid role name client-side without POSTing', async () => {
    const user = userEvent.setup()
    let posted = false
    server.use(
      permissionsHandler(),
      http.post('http://localhost/api/v1/roles', () => {
        posted = true
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    renderForm('/roles/new')

    await waitFor(() => expect(screen.getByText('reviews:read')).toBeInTheDocument())
    await user.type(screen.getByLabelText('Name'), 'Bad Name!')
    await user.type(screen.getByLabelText('Display name'), 'Bad')
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(screen.getByText(/lowercase letter first/i)).toBeInTheDocument())
    expect(posted).toBe(false)
  })

  it('hides non-delegable permissions from the picker', async () => {
    server.use(permissionsHandler())
    renderForm('/roles/new')

    await waitFor(() => expect(screen.getByText('reviews:read')).toBeInTheDocument())
    expect(screen.queryByText('roles:manage')).toBeNull()
    expect(screen.queryByText('users:manage_admin')).toBeNull()
  })
})

describe('RoleFormPage — edit', () => {
  it('edits a custom role and PATCHes the changed fields', async () => {
    const user = userEvent.setup()
    let body: { display_name?: string; permissions?: string[] } | null = null
    server.use(
      roleHandler(ROLE_CUSTOM),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:id', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({
          ...ROLE_CUSTOM,
          display_name: body!.display_name ?? ROLE_CUSTOM.display_name,
        })
      }),
    )
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    await user.clear(screen.getByLabelText('Display name'))
    await user.type(screen.getByLabelText('Display name'), 'Senior Reviewer')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.display_name).toBe('Senior Reviewer')
    expect(body!.permissions).toEqual(['reviews:read'])
  })

  it('sets a custom role as the new-user default', async () => {
    const user = userEvent.setup()
    let body: { is_default?: boolean } | null = null
    server.use(
      roleHandler(ROLE_CUSTOM),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({ ...ROLE_CUSTOM, is_default: body!.is_default ?? false })
      }),
    )
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    await user.click(screen.getByRole('checkbox', { name: /default role for new users/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.is_default).toBe(true)
  })

  it('reverts the flag when a guard rejects the change', async () => {
    const user = userEvent.setup()
    let gets = 0
    server.use(
      // A rejected write leaves the payload identical, so the refetch returns the same data —
      // structural sharing keeps the query reference and the form must be reset explicitly.
      http.get('http://localhost/api/v1/roles/:role_id', () => {
        gets += 1
        return HttpResponse.json(ROLE_CUSTOM)
      }),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', () =>
        HttpResponse.json(
          { title: 'Conflict', detail: 'Role cannot be made a default role.' },
          { status: 409 },
        ),
      ),
    )
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    const flag = screen.getByRole('checkbox', { name: /default role for new users/i })
    await user.click(flag)
    expect(flag).toBeChecked()

    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(gets).toBe(2))
    await waitFor(() => expect(flag).not.toBeChecked())
  })

  it('sets a custom role as the group self-join default', async () => {
    const user = userEvent.setup()
    let body: { is_participant_default?: boolean } | null = null
    server.use(
      roleHandler(ROLE_GRANTABLE),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({
          ...ROLE_GRANTABLE,
          is_participant_default: body!.is_participant_default ?? false,
        })
      }),
    )
    renderForm('/roles/r-gr/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('In Group'))
    await user.click(screen.getByRole('checkbox', { name: /default role for group self-join/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.is_participant_default).toBe(true)
  })

  it('opts a custom role into in-group assignment', async () => {
    const user = userEvent.setup()
    let body: { is_object_assignable?: boolean } | null = null
    server.use(
      roleHandler(ROLE_IN_GROUP),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({ ...ROLE_IN_GROUP, is_object_assignable: true })
      }),
    )
    renderForm('/roles/r-ig/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('In Group'))
    await user.click(screen.getByRole('checkbox', { name: /assignable inside evaluation groups/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.is_object_assignable).toBe(true)
  })

  it('enables in-group assignment as soon as group read is picked', async () => {
    const user = userEvent.setup()
    server.use(roleHandler(ROLE_CUSTOM), permissionsHandler())
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    const toggle = screen.getByRole('checkbox', { name: /assignable inside evaluation groups/i })
    expect(toggle).toBeDisabled()

    await user.click(screen.getByRole('checkbox', { name: /evaluation_groups:read/i }))

    await waitFor(() => expect(toggle).toBeEnabled())
  })

  it('blocks save when group read is dropped from an in-group assignable role', async () => {
    // The backend refuses this combination, so catch it before the request goes out.
    const user = userEvent.setup()
    let patched = false
    server.use(
      roleHandler(ROLE_GRANTABLE),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', async () => {
        patched = true
        return HttpResponse.json(ROLE_GRANTABLE)
      }),
    )
    renderForm('/roles/r-gr/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('In Group'))
    await user.click(screen.getByRole('checkbox', { name: /evaluation_groups:read/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(screen.getByText(/must grant evaluation_groups:read/i)).toBeInTheDocument(),
    )
    expect(patched).toBe(false)
  })

  it('blocks in-group assignment until the role grants group read', async () => {
    // Mirrors the backend guard client-side, so the operator sees why instead of a 409.
    server.use(roleHandler(ROLE_CUSTOM), permissionsHandler())
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    expect(
      screen.getByRole('checkbox', { name: /assignable inside evaluation groups/i }),
    ).toBeDisabled()
    // The slug sits in its own <span>, so match the hint by its prose and assert the slug on it.
    expect(screen.getByText(/to make this role assignable inside a group/i)).toHaveTextContent(
      'evaluation_groups:read',
    )
  })

  it('locks a system role out of field edits', async () => {
    server.use(roleHandler(ROLE_ADMIN), permissionsHandler())
    renderForm('/roles/r-admin/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toBeDisabled())
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /users:read/i })).toBeNull()
    // Object-assignability is code-managed for system roles, so it isn't offered.
    expect(
      screen.queryByRole('checkbox', { name: /assignable inside evaluation groups/i }),
    ).toBeNull()
  })

  it('deactivates a deactivatable system role', async () => {
    const user = userEvent.setup()
    let body: { is_active?: boolean } | null = null
    server.use(
      roleHandler(ROLE_VIEWER),
      permissionsHandler(),
      http.patch('http://localhost/api/v1/roles/:role_id', async ({ request }) => {
        body = (await request.json()) as typeof body
        return HttpResponse.json({
          ...ROLE_VIEWER,
          is_active: body!.is_active ?? ROLE_VIEWER.is_active,
        })
      }),
    )
    renderForm('/roles/r-viewer/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toBeDisabled())
    await user.click(screen.getByRole('checkbox', { name: /^active$/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.is_active).toBe(false)
  })

  it('deletes a custom role through the confirm dialog', async () => {
    const user = userEvent.setup()
    let deleted = false
    server.use(
      roleHandler(ROLE_CUSTOM),
      permissionsHandler(),
      http.delete('http://localhost/api/v1/roles/:id', () => {
        deleted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderForm('/roles/r-lead/edit')

    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue('Lead Reviewer'))
    await user.click(screen.getByRole('button', { name: /delete/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(deleted).toBe(true))
  })
})
