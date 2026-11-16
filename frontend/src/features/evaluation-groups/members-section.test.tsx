import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MembersSection } from './members-section'
import type { EvaluationGroupAccessLevel } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))

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

const USER_ID_3 = 'usr-0003-0000-0000-000000000000'

const user3 = {
  ...user1,
  id: USER_ID_3,
  email: 'carol@example.com',
  first_name: 'Carol',
  last_name: 'Danvers',
}

const user4 = {
  ...user1,
  id: 'usr-0004-0000-0000-000000000000',
  email: 'dave@example.com',
  first_name: 'Dave',
  last_name: 'Bowman',
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
    // AddMemberDialog queries (users only once it opens)
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/roles', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

function renderSection(
  groupPermissions: string[],
  globalPermissions: string[] = [],
  access: { accessLevel?: EvaluationGroupAccessLevel; organizationId?: string | null } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(globalPermissions)
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <MembersSection
            groupId={GROUP_ID}
            accessLevel={access.accessLevel ?? 'public'}
            organizationId={access.organizationId ?? null}
            userPermissions={groupPermissions}
          />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('MembersSection — eligible to join', () => {
  const ORG_ID = 'org-0001-0000-0000-000000000000'
  const orgHandler = http.get(`http://localhost/api/v1/organizations/${ORG_ID}`, () =>
    HttpResponse.json({ id: ORG_ID, name: 'Andless' }),
  )

  it('names the owning organization of an organization-access group', async () => {
    server.use(orgHandler, ...baseHandlers())
    renderSection(['evaluation_groups:read'], ['organizations:read'], {
      accessLevel: 'organization',
      organizationId: ORG_ID,
    })

    const section = await screen.findByRole('region', { name: /eligible to join on their own/i })
    expect(await within(section).findByText('Andless')).toBeInTheDocument()
  })

  it('still says an organization can join when its name cannot be read', async () => {
    server.use(orgHandler, ...baseHandlers())
    renderSection(['evaluation_groups:read'], [], {
      accessLevel: 'organization',
      organizationId: ORG_ID,
    })

    const section = await screen.findByRole('region', { name: /eligible to join on their own/i })
    expect(within(section).getByText('The owning organization')).toBeInTheDocument()
  })

  it.each(['public', 'invitation_only'] as const)(
    'has no such section for a %s group',
    async (level) => {
      server.use(orgHandler, ...baseHandlers())
      renderSection(['evaluation_groups:read'], ['organizations:read'], {
        accessLevel: level,
        organizationId: ORG_ID,
      })

      await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
      expect(screen.queryByRole('region', { name: /eligible to join/i })).toBeNull()
    },
  )
})

describe('MembersSection — renders', () => {
  it('renders members with name and role label', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
    expect(screen.getByText('Red Teamer')).toBeInTheDocument()
  })

  it('no longer lists annotators below the members', async () => {
    // The count is on the page's stat tile and the role shows in the list; the block is gone.
    let annotatorCalls = 0
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/annotators`, () => {
        annotatorCalls += 1
        return HttpResponse.json({ items: [annotator], total: 1, limit: 100, offset: 0 })
      }),
      ...baseHandlers(),
    )
    renderSection(['evaluation_groups:manage'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByText('Annotators')).toBeNull()
    expect(annotatorCalls).toBe(0)
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
    renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage'],
      ['users:read', 'roles:read'],
    )

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument()
  })

  it('with manage_members but no users:read (owner) — Invite + Edit shown, Add member hidden', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage_members'], ['roles:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /invite by email/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /edit roles/i })).toBeInTheDocument()
    // "Add existing user" needs the user picker (users:read) — owner can only invite by email.
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
  })

  it('with organizations:read on an organization group — Add member present without users:read', async () => {
    server.use(
      http.get('http://localhost/api/v1/organizations/org-0001-0000-0000-000000000000', () =>
        HttpResponse.json({ id: 'org-0001-0000-0000-000000000000', name: 'Andless' }),
      ),
      ...baseHandlers(),
    )
    renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage'],
      ['organizations:read', 'roles:read'],
      { accessLevel: 'organization', organizationId: 'org-0001-0000-0000-000000000000' },
    )

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument()
  })

  it('in-group owner without roles:read — no Invite, Add or Edit roles, whose role list would only 403', async () => {
    server.use(
      http.get('http://localhost/api/v1/organizations/org-0001-0000-0000-000000000000', () =>
        HttpResponse.json({ id: 'org-0001-0000-0000-000000000000', name: 'Andless' }),
      ),
      ...baseHandlers(),
    )
    renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage_members'],
      ['organizations:read', 'users:read'],
      { accessLevel: 'organization', organizationId: 'org-0001-0000-0000-000000000000' },
    )

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /remove member/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /invite by email/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /edit roles/i })).toBeNull()
  })

  it('with evaluation_groups:manage — Edit roles button present', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'], ['roles:read'])

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
  const ORG_ID = 'org-0001-0000-0000-000000000000'
  const annotatorRole = { ...role1, id: ROLE_ID_2, name: 'annotator', display_name: 'Annotator' }

  const participantDefault = { ...role1, is_participant_default: true }

  function handlers(opts: { users?: (typeof user1)[]; orgMembers?: (typeof user1)[] } = {}) {
    const posted: unknown[] = []
    const userQueries: URLSearchParams[] = []
    server.use(
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        userQueries.push(new URL(request.url).searchParams)
        const items = opts.users ?? [user3]
        return HttpResponse.json({ items, total: items.length, limit: 50, offset: 0 })
      }),
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({
          items: [participantDefault, annotatorRole],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.get(`http://localhost/api/v1/organizations/${ORG_ID}`, () =>
        HttpResponse.json({ id: ORG_ID, name: 'Andless' }),
      ),
      http.get(`http://localhost/api/v1/organizations/${ORG_ID}/members`, () => {
        const items = opts.orgMembers ?? []
        return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
      }),
      http.post(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/bulk`,
        async ({ request }) => {
          const body = (await request.json()) as { rows: { row_key: string }[] }
          posted.push(body)
          return HttpResponse.json({
            dry_run: false,
            total: body.rows.length,
            succeeded: body.rows.length,
            failed: 0,
            results: body.rows.map((r) => ({ row_key: r.row_key, status: 'ok', data: null })),
          })
        },
      ),
      ...baseHandlers(),
    )
    return { posted, userQueries }
  }

  async function openDialog(globalPermissions: string[], withOrg = false) {
    const user = userEvent.setup()
    renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage'],
      [...globalPermissions, 'roles:read'],
      withOrg ? { accessLevel: 'organization', organizationId: ORG_ID } : {},
    )
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add member/i }))
    return { user, dialog: screen.getByRole('dialog') }
  }

  it('adds the picked users in one bulk request, as Red Teamer unless changed', async () => {
    const { posted } = handlers({ users: [user3, user4] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByLabelText(/dave@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (2 selected)' }))
    await user.click(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Dave Bowman · dave@example.com',
      }),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Add 2' }))

    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toEqual({
      dry_run: false,
      rows: [
        { row_key: USER_ID_3, data: { user_id: USER_ID_3, role_ids: [ROLE_ID] } },
        { row_key: user4.id, data: { user_id: user4.id, role_ids: [ROLE_ID, ROLE_ID_2] } },
      ],
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('starts a picked user on the participant default, whichever role carries it', async () => {
    const { posted } = handlers({ users: [user3] })
    server.use(
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({
          items: [role1, { ...annotatorRole, is_participant_default: true }],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
    )
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add 1' }))

    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toEqual({
      dry_run: false,
      rows: [{ row_key: USER_ID_3, data: { user_id: USER_ID_3, role_ids: [ROLE_ID_2] } }],
    })
  })

  it('orders the role columns from least to most privileged', async () => {
    handlers({ users: [user3] })
    server.use(
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({
          items: [
            { ...role1, id: 'r-ann', name: 'annotator', display_name: 'Annotator' },
            { ...role1, id: 'r-own', name: 'owner', display_name: 'Owner' },
            role1,
            { ...role1, id: 'r-view', name: 'viewer', display_name: 'Viewer' },
          ],
          total: 4,
          limit: 100,
          offset: 0,
        }),
      ),
    )
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))

    expect(
      within(dialog)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toEqual(['User', 'Viewer', 'Red Teamer', 'Annotator', 'Owner'])
  })

  it('lists the organization first and adds all of it, skipping who is already a member', async () => {
    handlers({ users: [user3, user4], orgMembers: [user1, user4] })
    const { user, dialog } = await openDialog(['users:read', 'organizations:read'], true)

    const org = await within(dialog).findByRole('region', { name: 'Andless (organization)' })
    const others = within(dialog).getByRole('region', { name: 'Other users' })
    // Ada is a member already; Dave belongs to the organization, so he is listed once, under it.
    expect(await within(org).findByText('Already a member')).toBeInTheDocument()
    expect(within(others).queryByText(/dave@example.com/)).toBeNull()

    await user.click(within(org).getByLabelText('Add all members'))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))

    expect(
      within(dialog).getByRole('rowheader', { name: 'Dave Bowman · dave@example.com' }),
    ).toBeInTheDocument()
    expect(
      within(dialog).queryByRole('rowheader', { name: 'Ada Lovelace · ada@example.com' }),
    ).toBeNull()
  })

  it('sets a role for everyone from its column, and needs a role for each user', async () => {
    handlers({ users: [user3, user4] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByLabelText(/dave@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (2 selected)' }))

    await user.click(within(dialog).getByRole('checkbox', { name: 'Select all as Annotator' }))
    expect(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Carol Danvers · carol@example.com',
      }),
    ).toBeChecked()
    expect(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Dave Bowman · dave@example.com',
      }),
    ).toBeChecked()

    await user.click(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Carol Danvers · carol@example.com',
      }),
    )
    // Only Dave holds it now: the column toggle shows a partial state.
    expect(
      within(dialog).getByRole('checkbox', { name: 'Select all as Annotator' }),
    ).toHaveAttribute('data-state', 'indeterminate')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select all as Red Teamer' }))
    await user.click(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Dave Bowman · dave@example.com',
      }),
    )
    expect(within(dialog).getByRole('button', { name: 'Add 2' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Add 2' })).toHaveAccessibleDescription(
      'Give every user at least one role.',
    )
  })

  it('moves focus into each step instead of dropping it out of the dialog', async () => {
    handlers({ users: [user3] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))
    expect(within(dialog).getByRole('checkbox', { name: 'Select all as Red Teamer' })).toHaveFocus()

    await user.click(within(dialog).getByRole('button', { name: 'Back' }))
    expect(within(dialog).getByLabelText('Users')).toHaveFocus()
  })

  it('keeps the picks and their roles when going back a step', async () => {
    handlers({ users: [user3] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))
    await user.click(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Carol Danvers · carol@example.com',
      }),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Back' }))

    expect(within(dialog).getByLabelText(/carol@example.com/)).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))
    expect(
      within(dialog).getByRole('checkbox', {
        name: 'Annotator for Carol Danvers · carol@example.com',
      }),
    ).toBeChecked()
  })

  it('searches users on the server, ordered by email', async () => {
    const { userQueries } = handlers({ users: [user3] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.type(within(dialog).getByLabelText('Users'), 'car')

    await waitFor(() => expect(userQueries.at(-1)?.get('search')).toBe('car'))
    // Debounced: the unfiltered first page, then one search for the whole term, not one per keystroke.
    expect(userQueries.map((q) => q.get('search'))).toEqual([null, 'car'])
    expect(userQueries.at(-1)?.get('order_by')).toBe('email')
  })

  it('lists the rows that failed and keeps the dialog open', async () => {
    handlers({ users: [user3, user4] })
    server.use(
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/bulk`, () =>
        HttpResponse.json({
          dry_run: false,
          total: 2,
          succeeded: 1,
          failed: 1,
          results: [
            { row_key: USER_ID_3, status: 'ok', data: null },
            {
              row_key: user4.id,
              status: 'failed',
              error: { title: 'Conflict', status: 409, detail: 'Already a member of this group.' },
            },
          ],
        }),
      ),
    )
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByLabelText(/dave@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (2 selected)' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add 2' }))

    const status = await within(dialog).findByRole('status')
    expect(status).toHaveTextContent('Added 1 of 2')
    expect(status).toHaveTextContent('dave@example.com — Already a member')
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('loads the next page of users on demand', async () => {
    handlers()
    server.use(
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get('offset'))
        const items = offset === 0 ? [user3] : [user4]
        return HttpResponse.json({ items, total: 2, limit: 50, offset })
      }),
    )
    const { user, dialog } = await openDialog(['users:read'])

    expect(await within(dialog).findByLabelText(/carol@example.com/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Load more' }))

    expect(await within(dialog).findByLabelText(/dave@example.com/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('clears the typed term when the dialog is reopened', async () => {
    handlers({ users: [user3] })
    const { user, dialog } = await openDialog(['users:read'])

    await user.type(within(dialog).getByLabelText('Users'), 'car')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: /add member/i }))

    expect(within(screen.getByRole('dialog')).getByLabelText('Users')).toHaveValue('')
  })

  it('will not send more users than one add takes', async () => {
    const many = Array.from({ length: 1001 }, (_, i) => ({
      ...user1,
      id: `usr-many-${i}`,
      email: `m${i}@example.com`,
    }))
    handlers({ orgMembers: many })
    server.use(
      http.get(`http://localhost/api/v1/organizations/${ORG_ID}/members`, ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get('offset'))
        const items = many.slice(offset, offset + 100)
        return HttpResponse.json({ items, total: 1001, limit: 100, offset })
      }),
    )
    const { user, dialog } = await openDialog(['users:read', 'organizations:read'], true)
    const org = await within(dialog).findByRole('region', { name: 'Andless (organization)' })

    await user.click(await within(org).findByLabelText('Add all members'))
    await user.click(await within(dialog).findByLabelText(/carol@example.com/))

    expect(within(dialog).getByRole('button', { name: 'Next (1001 selected)' })).toBeDisabled()
    expect(within(dialog).getByText(/at most 1000 users/)).toBeInTheDocument()
  })

  it('stops paging the organization members when the dialog is cancelled', async () => {
    handlers({ users: [] })
    let aborted = false
    server.use(
      http.get(`http://localhost/api/v1/organizations/${ORG_ID}/members`, async ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get('offset'))
        if (offset === 0) {
          return HttpResponse.json({ items: [user3], total: 2, limit: 100, offset: 0 })
        }
        // The next page never answers on its own: only an abort ends it.
        await new Promise<void>((resolve) =>
          request.signal.addEventListener('abort', () => {
            aborted = true
            resolve()
          }),
        )
        return HttpResponse.error()
      }),
    )
    const { user, dialog } = await openDialog(['users:read', 'organizations:read'], true)

    await within(dialog).findByText('Loading members…')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(aborted).toBe(true))
  })

  it('does not request users until the dialog opens', async () => {
    const { userQueries } = handlers()
    renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage'],
      ['users:read', 'roles:read'],
    )

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(userQueries).toHaveLength(0)
  })

  it('marks a member from past the first page of the group as already a member', async () => {
    handlers({ users: [user3, user4] })
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get('offset') ?? 0)
        const later = { ...member, user: { ...member.user, id: user4.id, email: user4.email } }
        return HttpResponse.json({
          items: offset === 0 ? [member] : [later],
          total: 2,
          limit: 100,
          offset,
        })
      }),
    )
    const { dialog } = await openDialog(['users:read'])

    const dave = await within(dialog).findByRole('checkbox', { name: /dave@example.com/ })
    await waitFor(() => expect(dave).toBeDisabled())
    expect(within(dialog).getByRole('checkbox', { name: /carol@example.com/ })).toBeEnabled()
  })

  it('drops a member picked before the member list arrived', async () => {
    handlers({ users: [user1, user3] })
    let release: () => void = () => {}
    server.use(
      http.get(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`,
        async ({ request }) => {
          // Only the dialog's id walk pages by offset; hold it back so the pick lands first.
          if (new URL(request.url).searchParams.has('offset')) {
            await new Promise<void>((resolve) => (release = resolve))
          }
          return HttpResponse.json({ items: [member], total: 1, limit: 100, offset: 0 })
        },
      ),
    )
    const { user, dialog } = await openDialog(['users:read'])

    await user.click(await within(dialog).findByRole('checkbox', { name: /ada@example.com/ }))
    await user.click(within(dialog).getByRole('checkbox', { name: /carol@example.com/ }))
    release()

    expect(await within(dialog).findByRole('button', { name: 'Next (1 selected)' })).toBeEnabled()
  })

  it('toasts the outcome of a partly failed add when the page is gone before it lands', async () => {
    handlers({ users: [user3] })
    let respond: () => void = () => {}
    server.use(
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members/bulk`, async () => {
        await new Promise<void>((resolve) => (respond = resolve))
        return HttpResponse.json({
          dry_run: false,
          total: 1,
          succeeded: 0,
          failed: 1,
          results: [
            { row_key: USER_ID_3, status: 'failed', error: { title: 'Conflict', status: 409 } },
          ],
        })
      }),
    )
    const user = userEvent.setup()
    const { unmount } = renderSection(
      ['evaluation_groups:read', 'evaluation_groups:manage'],
      ['users:read', 'roles:read'],
    )
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add member/i }))
    const dialog = screen.getByRole('dialog')
    await user.click(await within(dialog).findByLabelText(/carol@example.com/))
    await user.click(within(dialog).getByRole('button', { name: 'Next (1 selected)' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add 1' }))

    unmount()
    respond()

    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('Added 0 of 1 members; 1 could not be added.'),
    )
  })

  it('says when the list shows only the first page of a large group', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () =>
        HttpResponse.json({ items: [member], total: 150, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
    )
    renderSection(['evaluation_groups:read'])

    expect(await screen.findByText('Showing the first 1 of 150 members.')).toBeInTheDocument()
  })
})

describe('MembersSection — bulk invite flow', () => {
  it('clicking Invite by email opens the bulk dialog with the emails textarea', async () => {
    server.use(...baseHandlers())

    const user = userEvent.setup()
    // manage_members (owner) grants invite without needing users:read; the role picker needs roles:read
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage_members'], ['roles:read'])

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

    await user.click(screen.getByRole('button', { name: 'Remove member' }))

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
    renderSection(['evaluation_groups:read', 'evaluation_groups:manage'], ['roles:read'])

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
