import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MembersSection } from './members-section'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const USER_ID = 'usr-0001-0000-0000-000000000000'
const USER_ID_2 = 'usr-0002-0000-0000-000000000000'
const ROLE_ID = 'rol-0001-0000-0000-000000000000'
const ROLE_ID_2 = 'rol-0002-0000-0000-000000000000'

const member = {
  user: {
    id: USER_ID,
    email: 'ada@example.com',
    first_name: 'Ada',
    last_name: 'Lovelace',
    status: 'active',
  },
  roles: [{ id: ROLE_ID, name: 'red_teamer', display_name: 'Red Teamer' }],
}

const annotator = {
  id: USER_ID_2,
  email: 'bob@example.com',
  first_name: 'Bob',
  last_name: null,
  status: 'active',
}

const user1 = {
  id: USER_ID,
  email: 'ada@example.com',
  first_name: 'Ada',
  last_name: 'Lovelace',
  status: 'active',
  provider: 'local',
  email_verified: true,
  roles: [],
  permissions: [],
}

const role1 = {
  id: ROLE_ID,
  name: 'red_teamer',
  display_name: 'Red Teamer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
}

function baseHandlers() {
  return [
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () =>
      HttpResponse.json({ items: [member], total: 1, limit: 100, offset: 0 }),
    ),
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/annotators`, () =>
      HttpResponse.json({ items: [annotator], total: 1, limit: 100, offset: 0 }),
    ),
    // AddMemberDialog always mounts these queries
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/roles', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

function renderSection(groupPermissions: string[], globalPermissions: string[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(globalPermissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <MembersSection
            groupId={GROUP_ID}
            accessLevel="public"
            userPermissions={groupPermissions}
          />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('MembersSection — renders', () => {
  it('renders members with name and role label', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByText('Red Teamer')).toBeInTheDocument()
  })

  it('renders annotator display name when the caller can manage members', async () => {
    server.use(...baseHandlers())
    // The annotator list is manage-members-gated; a manager sees it.
    renderSection(['evaluation_groups:manage'])

    // first_name='Bob', last_name=null → displayName returns 'Bob'
    await waitFor(() => expect(screen.getByText('Bob')).toBeInTheDocument())
  })

  it('hides the Annotators section for a member who cannot manage members', async () => {
    // The annotator endpoint is manage-members-gated server-side, so a plain
    // member must neither see the section nor fire the request (which would 403).
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByText('Annotators')).toBeNull()
    expect(screen.queryByText('Bob')).toBeNull()
  })
})

describe('MembersSection — RBAC gates', () => {
  it('with evaluation_groups:read only — no Add, Edit or Remove affordances', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /edit roles/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /remove/i })).toBeNull()
  })

  it('with evaluation_groups:manage + users:read — Add member button present', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'], ['users:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument()
  })

  it('with manage_members but no users:read (owner) — Invite + Edit shown, Add member hidden', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage_members'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /invite by email/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /edit roles/i })).toBeInTheDocument()
    // "Add existing user" needs the user picker (users:read) — owner can only invite by email.
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
  })

  it('with evaluation_groups:manage — Edit roles button present', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /edit roles/i })).toBeInTheDocument()
  })

  it('global manage without in-group authority — no management affordances (no over-granting)', async () => {
    // The caller holds evaluation_groups:manage globally but no in-group authority
    // on this group, so the server would reject member writes — the UI gates on the
    // group's user_permissions (empty here), not the global set.
    server.use(...baseHandlers())
    renderSection([], ['evaluation_groups:manage', 'users:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /invite by email/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /edit roles/i })).toBeNull()
  })
})

describe('MembersSection — add flow', () => {
  it('opens modal, picks user+role, submits POST with user_id and role_ids', async () => {
    let captured: unknown = null

    server.use(
      // override users/roles before baseHandlers so these take precedence (first-match wins)
      http.get('http://localhost/api/v1/auth/users', () =>
        HttpResponse.json({ items: [user1], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({ items: [role1], total: 1, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(member, { status: 201 })
        },
      ),
    )

    const user = userEvent.setup()
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'], ['users:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /add member/i }))

    const dialog = screen.getByRole('dialog')

    // Radix portals the listbox out of the dialog, so the option is queried from the document.
    await user.click(within(dialog).getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: 'ada@example.com' }))

    // pick role checkbox
    await waitFor(() => expect(within(dialog).getByLabelText('Red Teamer')).toBeInTheDocument())
    await user.click(within(dialog).getByLabelText('Red Teamer'))

    await user.click(within(dialog).getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(captured).not.toBeNull())

    const body = captured as { user_id: string; role_ids: string[] }
    expect(body.user_id).toBe(USER_ID)
    expect(body.role_ids).toContain(ROLE_ID)
  })
})

describe('MembersSection — bulk invite flow', () => {
  it('clicking Invite by email opens the bulk dialog with the emails textarea', async () => {
    server.use(...baseHandlers())

    const user = userEvent.setup()
    // manage_members (owner) grants invite without needing users:read
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage_members'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /invite by email/i }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByLabelText(/emails/i)).toBeInTheDocument()
  })
})

describe('MembersSection — remove flow', () => {
  it('clicking Remove and confirming calls DELETE', async () => {
    let deleteCalled = false

    server.use(
      ...baseHandlers(),
      http.delete(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/${USER_ID}`,
        () => {
          deleteCalled = true
          return new HttpResponse(null, { status: 204 })
        },
      ),
    )

    const user = userEvent.setup()
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    // click the trash button (second icon button) in the member row
    const memberRow = screen.getByText('Ada Lovelace').closest<HTMLElement>('.rounded-md')
    expect(memberRow).not.toBeNull()
    const trashBtn = within(memberRow!).getAllByRole('button')[1]
    expect(trashBtn).not.toBeNull()
    await user.click(trashBtn!)

    // confirm in the ConfirmDialog
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(deleteCalled).toBe(true))
  })
})

const role2 = {
  id: ROLE_ID_2,
  name: 'reviewer',
  display_name: 'Reviewer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
}

describe('MembersSection — edit flow', () => {
  it('opens edit dialog pre-filled with current roles, submit sends PATCH with new role_ids', async () => {
    let captured: unknown = null

    server.use(
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({ items: [role1, role2], total: 2, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
      http.patch(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/${USER_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({
            ...member,
            roles: [{ id: ROLE_ID_2, name: 'reviewer', label: 'Reviewer' }],
          })
        },
      ),
    )

    const user = userEvent.setup()
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /edit roles/i }))

    const dialog = screen.getByRole('dialog')

    // wait for roles to load, current role should be pre-checked
    await waitFor(() => expect(within(dialog).getByLabelText('Red Teamer')).toBeInTheDocument())
    expect(within(dialog).getByLabelText('Red Teamer')).toBeChecked()

    // uncheck current role, check new role
    await user.click(within(dialog).getByLabelText('Red Teamer'))
    await waitFor(() => expect(within(dialog).getByLabelText('Reviewer')).toBeInTheDocument())
    await user.click(within(dialog).getByLabelText('Reviewer'))

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())

    const body = captured as { role_ids: string[] }
    expect(body.role_ids).toContain(ROLE_ID_2)
    expect(body.role_ids).not.toContain(ROLE_ID)
  })

  it('edit dialog is hidden without evaluation_groups:manage', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /edit roles/i })).toBeNull()
  })
})
