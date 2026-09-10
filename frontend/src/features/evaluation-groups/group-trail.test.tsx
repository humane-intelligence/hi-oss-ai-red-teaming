import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { NavTrailSlotContext } from '@/app/layout/nav-trail-slot'
import { GroupIdentity, GroupIdentityHeading } from './group-trail'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const ORG_ID = 'org-0001-0000-0000-000000000000'
// Deliberately unlike the group's title: an assertion cannot tell the two apart otherwise.
const ORG_NAME = 'Acme Health'
const GROUP_TITLE = 'Spring Jailbreak Sprint'

function groupHandler(organizationId: string | null) {
  return http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
    HttpResponse.json({
      id: GROUP_ID,
      title: GROUP_TITLE,
      description: null,
      status: 'published',
      access_level: 'public',
      organization_id: organizationId,
      metrics_access_during: 'owner_only',
      metrics_access_after: 'owner_only',
      start_date: null,
      end_date: null,
      data_license_id: null,
      created_by_id: 'user-0001',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }),
  )
}

// The trail asks for the one organization it names, so the fixture is that one — not a list the
// group's own organization could fall off the end of.
function orgHandler(name: string | null, onCall?: () => void) {
  return http.get(`http://localhost/api/v1/organizations/${ORG_ID}`, () => {
    onCall?.()
    return name === null
      ? new HttpResponse(null, { status: 404 })
      : HttpResponse.json({
          id: ORG_ID,
          name,
          description: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        })
  })
}

function renderTrail(node: React.ReactElement, permissions = ['organizations:read']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>{node}</MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('GroupIdentity', () => {
  it('names the organization that owns the group, ahead of the group itself', async () => {
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    renderTrail(<GroupIdentity groupId={GROUP_ID} />)

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(nav).findByRole('link', { name: ORG_NAME })).toHaveAttribute(
      'href',
      `/organizations/${ORG_ID}`,
    )
    expect(await within(nav).findByRole('link', { name: GROUP_TITLE })).toHaveAttribute(
      'href',
      `/evaluation-groups/${GROUP_ID}`,
    )
    expect(within(nav).getByRole('link', { name: 'Evaluation Groups' })).toHaveAttribute(
      'href',
      '/evaluation-groups',
    )
  })

  it('fills the header slot and keeps a page copy for the widths the bar cannot carry', async () => {
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    const slot = document.createElement('div')
    slot.setAttribute('data-testid', 'header-slot')
    document.body.appendChild(slot)
    const body = document.createElement('div')
    document.body.appendChild(body)

    renderTrail(
      <NavTrailSlotContext.Provider value={slot}>
        <div data-testid="page-body">
          <GroupIdentity groupId={GROUP_ID} />
        </div>
      </NavTrailSlotContext.Provider>,
    )

    // Both exist by design and never show together: the page copy is `lg:hidden`, the header copy
    // `hidden lg:block`. Which one is visible is CSS, so it belongs to the browser pass, not jsdom —
    // asserting it here would only pin that a class string survived `cn()`.
    const navs = await screen.findAllByRole('navigation', { name: 'Evaluation group' })
    expect(navs).toHaveLength(2)
    expect(navs.some((nav) => slot.contains(nav))).toBe(true)
    expect(navs.some((nav) => screen.getByTestId('page-body').contains(nav))).toBe(true)
  })

  it('keeps the line in the page body when no slot is offered', async () => {
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    renderTrail(
      <div data-testid="page-body">
        <GroupIdentity groupId={GROUP_ID} />
      </div>,
    )

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(screen.getByTestId('page-body')).toContainElement(nav)
  })

  it('leaves the organization out for a group that has none, rather than inventing a placeholder', async () => {
    server.use(groupHandler(null), orgHandler(ORG_NAME))
    renderTrail(<GroupIdentity groupId={GROUP_ID} />)

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(nav).findByRole('link', { name: GROUP_TITLE })).toBeInTheDocument()
    expect(within(nav).queryByText(/organization/i)).toBeNull()
    expect(within(nav).queryByRole('link', { name: ORG_NAME })).toBeNull()
  })

  it('leaves it out when it cannot be named, rather than printing an id fragment', async () => {
    // The group points at an organization the caller cannot read.
    server.use(groupHandler(ORG_ID), orgHandler(null))
    renderTrail(<GroupIdentity groupId={GROUP_ID} />)

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(nav).findByRole('link', { name: GROUP_TITLE })).toBeInTheDocument()
    expect(within(nav).queryByText(/org-0001/)).toBeNull()
    expect(within(nav).queryByText(/…/)).toBeNull()
  })

  it('names the group without waiting on the organization, which only decorates it', async () => {
    server.use(
      groupHandler(ORG_ID),
      // Never settles: if the trail waited on it, the group's own name would be withheld too.
      http.get(
        `http://localhost/api/v1/organizations/${ORG_ID}`,
        () => new Promise<never>(() => {}),
      ),
    )
    renderTrail(<GroupIdentity groupId={GROUP_ID} />)

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(nav).findByRole('link', { name: GROUP_TITLE })).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()
  })

  it('asks for no organization at all without organizations:read, and still names the group', async () => {
    let orgCalls = 0
    server.use(
      groupHandler(ORG_ID),
      orgHandler(ORG_NAME, () => {
        orgCalls += 1
      }),
    )
    renderTrail(<GroupIdentity groupId={GROUP_ID} />, ['evaluation_groups:read'])

    const nav = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(nav).findByRole('link', { name: GROUP_TITLE })).toBeInTheDocument()
    expect(within(nav).queryByRole('link', { name: ORG_NAME })).toBeNull()
    expect(orgCalls).toBe(0)
  })

  it('says the names are still loading rather than naming the group wrong', async () => {
    server.use(
      // Never settles: the trail stays in flight for the whole test.
      http.get(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}`,
        () => new Promise<never>(() => {}),
      ),
      orgHandler(ORG_NAME),
    )
    renderTrail(<GroupIdentity groupId={GROUP_ID} />)

    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: GROUP_TITLE })).toBeNull()
  })
})

describe('GroupIdentityHeading', () => {
  it('renders the identity as the page heading, with the organization linked inside it', async () => {
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    renderTrail(<GroupIdentityHeading groupId={GROUP_ID} />)

    const heading = await screen.findByRole('heading', { level: 1 })
    expect(await within(heading).findByRole('link', { name: ORG_NAME })).toBeInTheDocument()
    expect(heading).toHaveTextContent(GROUP_TITLE)
    // The page you are on does not link to itself.
    expect(within(heading).queryByRole('link', { name: GROUP_TITLE })).toBeNull()
  })

  it('renders the heading alone, leaving the way back to the list to the page', async () => {
    // The page owns that landmark so it can sit above the badge row: a two-row block dropped into a
    // centred flex row pulls the badges up beside the crumb instead of the title.
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    renderTrail(<GroupIdentityHeading groupId={GROUP_ID} />)

    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Evaluation groups list' })).toBeNull()
    expect(
      within(await screen.findByRole('heading', { level: 1 })).queryByText('Evaluation Groups'),
    ).toBeNull()
  })

  it('fills the header slot too, so the bar carries the same context as every other page', async () => {
    server.use(groupHandler(ORG_ID), orgHandler(ORG_NAME))
    const slot = document.createElement('div')
    document.body.appendChild(slot)

    renderTrail(
      <NavTrailSlotContext.Provider value={slot}>
        <GroupIdentityHeading groupId={GROUP_ID} />
      </NavTrailSlotContext.Provider>,
    )

    const inHeader = await within(slot).findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(inHeader).findByRole('link', { name: ORG_NAME })).toBeInTheDocument()
    // Distinct landmark names: the heading's own way-back nav must not answer to the same name.
    expect(within(slot).queryByRole('navigation', { name: 'Evaluation groups list' })).toBeNull()
  })

  it('heads the page from the first frame, before any name has landed', async () => {
    server.use(
      http.get(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}`,
        () => new Promise<never>(() => {}),
      ),
      orgHandler(ORG_NAME),
    )
    renderTrail(<GroupIdentityHeading groupId={GROUP_ID} />)

    // The slot keeps its size and the page keeps a heading: an `h1` that arrives with the data
    // moves everything below it.
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument()
  })
})
