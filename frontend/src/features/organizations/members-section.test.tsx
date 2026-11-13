import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MembersSection } from './members-section'

const ORG_ID = 'org-0001-0000-0000-000000000000'
const USER_ID = 'usr-0001-0000-0000-000000000000'
const USER_ID_2 = 'usr-0002-0000-0000-000000000000'
const USER_ID_3 = 'usr-0003-0000-0000-000000000000'

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

const nameless = {
  ...user1,
  id: USER_ID_2,
  email: 'bob@example.com',
  first_name: null,
  last_name: null,
}

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

// Ada as the users list returns her: already in this organization.
const adaInOrg = { ...user1, organization: { id: ORG_ID, name: 'Acme' } }
// Carol belongs to another organization — still pickable, assigning moves her.
const carolElsewhere = {
  ...user3,
  organization: { id: 'org-0002-0000-0000-000000000000', name: 'Other' },
}

// jsdom has no layout: give the listbox the geometry of one scrolled to its end.
function scrollListToEnd() {
  const list = screen.getByRole('listbox')
  Object.defineProperty(list, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(list, 'clientHeight', { value: 200, configurable: true })
  Object.defineProperty(list, 'scrollTop', { value: 800, configurable: true })
  fireEvent.scroll(list)
}

const MEMBERS_URL = `http://localhost/api/v1/organizations/${ORG_ID}/members`

function baseHandlers() {
  return [
    http.get(MEMBERS_URL, () =>
      HttpResponse.json({ items: [user1, nameless], total: 2, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
    ),
  ]
}

function renderSection(globalPermissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(globalPermissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <MembersSection organizationId={ORG_ID} />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

const MANAGER = ['organizations:read', 'organizations:manage_members', 'users:read']

describe('Organization MembersSection — renders', () => {
  it('shows the email under the name, and a nameless member once', async () => {
    server.use(...baseHandlers())
    renderSection(['organizations:read'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
    expect(screen.getAllByText('bob@example.com')).toHaveLength(1)
  })
})

describe('Organization MembersSection — RBAC gates', () => {
  it('without users:read — no Add member button', async () => {
    server.use(...baseHandlers())
    renderSection(['organizations:read', 'organizations:manage_members'])

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
  })

  it('with manage_members + users:read — Add member button present', async () => {
    server.use(...baseHandlers())
    renderSection(MANAGER)

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument()
  })
})

describe('Organization MembersSection — add flow', () => {
  it('searches on the server, disables members, and submits POST with user_id', async () => {
    let captured: unknown = null
    const searches: (string | null)[] = []

    server.use(
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        const search = new URL(request.url).searchParams.get('search')
        searches.push(search)
        const items = search === 'a' ? [adaInOrg, carolElsewhere] : []
        return HttpResponse.json({ items, total: items.length, limit: 50, offset: 0 })
      }),
      ...baseHandlers(),
      http.post(MEMBERS_URL, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json(user3, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderSection(MANAGER)
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /add member/i }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByRole('combobox', { name: 'User' }), 'a')

    const carol = await screen.findByRole('option', { name: 'Carol Danvers · carol@example.com' })
    expect(searches).toContain('a')
    expect(carol).not.toHaveAttribute('aria-disabled')
    expect(
      screen.getByRole('option', { name: 'Ada Lovelace · ada@example.com Already a member' }),
    ).toHaveAttribute('aria-disabled', 'true')

    await user.click(carol)
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(captured).toEqual({ user_id: USER_ID_3 }))
  })

  it('loads the next page when the list is scrolled to the end, and stops at the total', async () => {
    const offsets: (string | null)[] = []
    server.use(
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        const offset = new URL(request.url).searchParams.get('offset')
        offsets.push(offset)
        const items = offset === '0' ? [user3] : [user4]
        return HttpResponse.json({ items, total: 2, limit: 50, offset: Number(offset) })
      }),
      ...baseHandlers(),
    )

    const user = userEvent.setup()
    renderSection(MANAGER)
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('combobox', { name: 'User' }))
    await screen.findByRole('option', { name: 'Carol Danvers · carol@example.com' })

    scrollListToEnd()
    expect(
      await screen.findByRole('option', { name: 'Dave Bowman · dave@example.com' }),
    ).toBeInTheDocument()

    scrollListToEnd()
    await new Promise((r) => setTimeout(r, 50))
    expect(offsets).toEqual(['0', '1'])
  })

  it('does not request users until the dialog opens', async () => {
    let requests = 0
    server.use(
      http.get('http://localhost/api/v1/auth/users', () => {
        requests += 1
        return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 })
      }),
      ...baseHandlers(),
    )

    const user = userEvent.setup()
    renderSection(MANAGER)
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(requests).toBe(0)

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await waitFor(() => expect(requests).toBe(1))
  })

  it('disables Add once the picked user is edited away', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/users', () =>
        HttpResponse.json({ items: [user3], total: 1, limit: 50, offset: 0 }),
      ),
      ...baseHandlers(),
    )

    const user = userEvent.setup()
    renderSection(MANAGER)
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add member/i }))
    const dialog = screen.getByRole('dialog')
    const combobox = within(dialog).getByRole('combobox', { name: 'User' })

    await user.click(combobox)
    await user.click(
      await screen.findByRole('option', { name: 'Carol Danvers · carol@example.com' }),
    )
    expect(within(dialog).getByRole('button', { name: 'Add' })).toBeEnabled()

    await user.type(combobox, 'x')
    expect(within(dialog).getByRole('button', { name: 'Add' })).toBeDisabled()
  })

  it('clears the typed term when the dialog is reopened, without fetching it again', async () => {
    const searches: (string | null)[] = []
    server.use(
      http.get('http://localhost/api/v1/auth/users', ({ request }) => {
        searches.push(new URL(request.url).searchParams.get('search'))
        return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 })
      }),
      ...baseHandlers(),
    )

    const user = userEvent.setup()
    renderSection(MANAGER)
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(
      within(screen.getByRole('dialog')).getByRole('combobox', { name: 'User' }),
      'car',
    )
    await waitFor(() => expect(searches).toContain('car'))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    const beforeReopen = searches.length

    await user.click(screen.getByRole('button', { name: /add member/i }))
    expect(within(screen.getByRole('dialog')).getByRole('combobox', { name: 'User' })).toHaveValue(
      '',
    )
    await waitFor(() => expect(searches.slice(beforeReopen)).toContain(null))
    expect(searches.slice(beforeReopen)).not.toContain('car')
  })
})
