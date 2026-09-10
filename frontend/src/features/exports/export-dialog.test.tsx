import { describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ExportDialog } from './export-dialog'
import type { ExportScope } from './queries'
import { ExportsContext, type ExportsContextValue } from './exports-context'
import type { CsvExportInfo } from '@/lib/api/types'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const EVAL_ID = 'eval-0001-0000-0000-000000000000'

const TRANSCRIPT: CsvExportInfo = {
  key: 'transcript',
  name: 'Transcript',
  description: 'Every message, one row per turn.',
  permission: 'conversations:read',
}
const FLAGS: CsvExportInfo = {
  key: 'flags',
  name: 'Flags',
  description: 'Flagged messages.',
  permission: 'flags:read',
}

function templatesHandler(items: CsvExportInfo[]) {
  return http.get('http://localhost/api/v1/exports', () =>
    HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 }),
  )
}

function renderDialog(
  perms: string[],
  {
    trackExport = vi.fn(),
    onOpenChange = vi.fn(),
    scope = { evaluation_group_id: GROUP_ID },
  }: { trackExport?: () => void; onOpenChange?: (o: boolean) => void; scope?: ExportScope } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  // Override useExports with a spy so the dialog doesn't hand jobs to the real
  // background poller (keeps the test to the dialog's own behaviour).
  const ctx: ExportsContextValue = { trackExport, activeCount: 0 }
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ExportsContext.Provider value={ctx}>
            <ExportDialog open onOpenChange={onOpenChange} scope={scope} title="Export" />
          </ExportsContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ExportDialog', () => {
  it('offers every catalog template (export is gated on owner authority, not per-template read perms)', async () => {
    // A group owner's global role need not hold each template's data-read permission, yet the
    // server authorises the whole-group extract — so the picker must not filter by that permission.
    server.use(templatesHandler([TRANSCRIPT, FLAGS]))

    renderDialog([])

    expect(await screen.findByRole('radio', { name: /transcript/i })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /flags/i })).toBeInTheDocument()
  })

  it('shows a loading state (not an empty/permission message) while templates load', async () => {
    server.use(
      http.get('http://localhost/api/v1/exports', async () => {
        await delay('infinite')
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
    )

    renderDialog([])

    expect(await screen.findByText(/loading templates/i)).toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
  })

  it('queues a job with the scope + a fresh idempotency key, tracks it, and closes', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      templatesHandler([TRANSCRIPT]),
      http.post('http://localhost/api/v1/exports/jobs', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          {
            id: 'job-1',
            template: 'transcript',
            status: 'pending',
            evaluation_id: null,
            evaluation_group_id: GROUP_ID,
            error: null,
            created_at: '2026-01-01T00:00:00Z',
            expires_at: null,
          },
          { status: 202 },
        )
      }),
    )
    const trackExport = vi.fn()
    const onOpenChange = vi.fn()
    const user = userEvent.setup()

    renderDialog(['conversations:read'], { trackExport, onOpenChange })

    await user.click(await screen.findByRole('radio', { name: /transcript/i }))
    await user.click(screen.getByRole('button', { name: /^export$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({
      template: 'transcript',
      format: 'csv',
      evaluation_group_id: GROUP_ID,
    })
    expect(typeof body!.idempotency_key).toBe('string')
    expect((body!.idempotency_key as string).length).toBeGreaterThan(0)

    await waitFor(() => expect(trackExport).toHaveBeenCalledTimes(1))
    expect(trackExport).toHaveBeenCalledWith(
      { id: 'job-1', template: 'transcript' },
      `evaluation-group-${GROUP_ID}`,
    )
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('sends the chosen format and only the set filters', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      templatesHandler([TRANSCRIPT]),
      // The picker's user select (group scope) lists members; return an empty list.
      http.get('http://localhost/api/v1/evaluation-groups/:id/members', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/exports/jobs', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          {
            id: 'job-2',
            template: 'transcript',
            status: 'pending',
            evaluation_id: null,
            evaluation_group_id: GROUP_ID,
            error: null,
            created_at: '2026-01-01T00:00:00Z',
            expires_at: null,
          },
          { status: 202 },
        )
      }),
    )
    const user = userEvent.setup()
    renderDialog(['conversations:read'])

    await user.click(await screen.findByRole('radio', { name: /transcript/i }))
    await user.click(screen.getByRole('radio', { name: /json/i }))
    fireEvent.change(screen.getByLabelText(/created from/i), { target: { value: '2026-03-01' } })
    await user.click(screen.getByRole('button', { name: /^export$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    // Format carried; the one set date filter carried (the picked local day as a UTC instant);
    // unset filters omitted. Compute the expected value the same way so the assertion is TZ-robust.
    const expectedFrom = new Date('2026-03-01T00:00:00').toISOString()
    expect(body).toMatchObject({
      template: 'transcript',
      format: 'json',
      filters: { created_from: expectedFrom },
    })
    expect(body!.filters).not.toHaveProperty('created_to')
    expect(body!.filters).not.toHaveProperty('status')
  })

  it('offers the Red-teamer filter for the conversation-groups export (catalog key matches USER_TEMPLATES)', async () => {
    // Regression: USER_TEMPLATES must carry the hyphenated catalog key 'conversation-groups' — with
    // the underscore typo the picker silently never rendered the user filter for this export.
    const CONVERSATION_GROUPS: CsvExportInfo = {
      key: 'conversation-groups',
      name: 'Conversation groups',
      description: 'Conversation groups.',
      permission: 'conversations:read',
    }
    server.use(
      templatesHandler([CONVERSATION_GROUPS]),
      http.get('http://localhost/api/v1/evaluation-groups/:id/members', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    const user = userEvent.setup()
    renderDialog(['conversations:read', 'roles:read'])

    await user.click(await screen.findByRole('radio', { name: /conversation groups/i }))
    expect(await screen.findByLabelText('Red-teamer')).toBeInTheDocument()
  })

  it('blocks an inverted date range instead of queueing an empty export', async () => {
    server.use(templatesHandler([TRANSCRIPT]))
    const user = userEvent.setup()
    renderDialog(['conversations:read'])

    await user.click(await screen.findByRole('radio', { name: /transcript/i }))
    fireEvent.change(screen.getByLabelText(/created from/i), { target: { value: '2026-03-10' } })
    fireEvent.change(screen.getByLabelText(/created to/i), { target: { value: '2026-03-01' } })

    expect(screen.getByText(/must be on or before/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^export$/i })).toBeDisabled()
  })

  // One case per catalog template: guards against a typo in any STATUS/SCENARIO/TASK/USER Set
  // silently hiding an applicable filter (the conversation-groups hyphen regression above was one).
  const info = (key: string, name: string): CsvExportInfo => ({
    key,
    name,
    description: `${name}.`,
    permission: 'conversations:read',
  })
  const TEMPLATE_MATRIX = [
    { key: 'flags', name: 'Flags', status: true, user: true, scenario: true },
    { key: 'reviews', name: 'Reviews', status: true, user: false, scenario: false },
    { key: 'conversations', name: 'Conversations', status: false, user: true, scenario: true },
    {
      key: 'conversation-groups',
      name: 'Conversation groups',
      status: false,
      user: true,
      scenario: false,
    },
    { key: 'transcript', name: 'Transcript', status: false, user: true, scenario: true },
    {
      key: 'engagement_report',
      name: 'Engagement report',
      status: false,
      user: true,
      scenario: true,
    },
  ]

  describe.each(TEMPLATE_MATRIX)(
    'group-scope filter controls · $key',
    ({ key, name, status, user }) => {
      it('surfaces Status/Red-teamer exactly where the template supports them', async () => {
        server.use(
          templatesHandler([info(key, name)]),
          http.get('http://localhost/api/v1/evaluation-groups/:id/members', () =>
            HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
          ),
        )
        const u = userEvent.setup()
        renderDialog(['flags:read', 'reviews:read', 'conversations:read', 'roles:read'])
        await u.click(await screen.findByRole('radio', { name: new RegExp(name, 'i') }))

        expect(Boolean(screen.queryByLabelText('Status'))).toBe(status)
        // The Red-teamer picker appears only after the async /roles fetch resolves the role id, so
        // await its presence; assert its absence synchronously (a findBy would burn the full timeout).
        if (user) {
          expect(await screen.findByLabelText('Red-teamer')).toBeInTheDocument()
        } else {
          expect(screen.queryByLabelText('Red-teamer')).not.toBeInTheDocument()
        }
        // scenario/task need an evaluation scope, never a group one
        expect(screen.queryByLabelText('Scenario')).not.toBeInTheDocument()
      })
    },
  )

  describe.each(TEMPLATE_MATRIX)(
    'evaluation-scope filter controls · $key',
    ({ key, name, scenario }) => {
      it('surfaces Scenario exactly where the template supports it', async () => {
        server.use(
          templatesHandler([info(key, name)]),
          http.get('http://localhost/api/v1/evaluations/:id/scenarios', () =>
            HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
          ),
        )
        const u = userEvent.setup()
        renderDialog(['flags:read', 'reviews:read', 'conversations:read'], {
          scope: { evaluation_id: EVAL_ID },
        })
        await u.click(await screen.findByRole('radio', { name: new RegExp(name, 'i') }))

        expect(Boolean(screen.queryByLabelText('Scenario'))).toBe(scenario)
        // the user filter needs a group scope, never an evaluation one
        expect(screen.queryByLabelText('Red-teamer')).not.toBeInTheDocument()
      })
    },
  )

  it('reveals the Task filter only for the flags export, once a scenario is chosen', async () => {
    server.use(
      templatesHandler([info('flags', 'Flags')]),
      http.get('http://localhost/api/v1/evaluations/:id/scenarios', () =>
        HttpResponse.json({
          items: [{ id: 'sc-1', name: 'Scenario 1' }],
          total: 1,
          limit: 100,
          offset: 0,
        }),
      ),
      http.get('http://localhost/api/v1/scenarios/:id/tasks', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    const u = userEvent.setup()
    renderDialog(['flags:read'], { scope: { evaluation_id: EVAL_ID } })
    await u.click(await screen.findByRole('radio', { name: /flags/i }))

    expect(screen.queryByLabelText('Task')).not.toBeInTheDocument() // no scenario picked yet
    await u.click(await screen.findByLabelText('Scenario'))
    await u.click(await screen.findByRole('option', { name: 'Scenario 1' }))
    expect(await screen.findByLabelText('Task')).toBeInTheDocument()
  })

  it('red-teamer picker searches members server-side, filtered to the red_teamer role', async () => {
    let searchParam: string | null = null
    let roleIdParam: string | null = null
    server.use(
      templatesHandler([info('flags', 'Flags')]),
      http.get('http://localhost/api/v1/evaluation-groups/:id/members', ({ request }) => {
        const q = new URL(request.url).searchParams
        searchParam = q.get('search')
        roleIdParam = q.get('role_id')
        return HttpResponse.json({
          items: [{ user: { id: 'u1', email: 'alice@ex.com' }, roles: [] }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }),
    )
    const u = userEvent.setup()
    renderDialog(['flags:read', 'roles:read'])
    await u.click(await screen.findByRole('radio', { name: /flags/i }))
    fireEvent.change(await screen.findByLabelText('Red-teamer'), { target: { value: 'ali' } })

    await waitFor(() => expect(searchParam).toBe('ali'))
    expect(roleIdParam).toBe('role-red-teamer') // resolved from the default /roles handler
  })

  it('hides the red-teamer picker (and never fetches members) when no red_teamer role id resolves', async () => {
    const membersSpy = vi.fn()
    server.use(
      templatesHandler([info('flags', 'Flags')]),
      http.get(
        'http://localhost/api/v1/roles',
        () => HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }), // no red_teamer role
      ),
      http.get('http://localhost/api/v1/evaluation-groups/:id/members', () => {
        membersSpy()
        return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })
      }),
    )
    const u = userEvent.setup()
    renderDialog(['flags:read', 'roles:read'])
    await u.click(await screen.findByRole('radio', { name: /flags/i }))

    // No red_teamer id → the picker can't role-filter, so it's hidden and members are never fetched.
    await waitFor(() => expect(membersSpy).not.toHaveBeenCalled())
    expect(screen.queryByLabelText('Red-teamer')).not.toBeInTheDocument()
  })

  it('hides the red-teamer picker when the caller lacks roles:read (no /roles request, no 403)', async () => {
    // Export authority is object-scoped; GET /roles needs global roles:read. Without it the role id
    // can't resolve — hide the picker rather than fire a 403 + error toast and leave a dead empty box.
    const rolesSpy = vi.fn()
    server.use(
      templatesHandler([info('flags', 'Flags')]),
      http.get('http://localhost/api/v1/roles', () => {
        rolesSpy()
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
    )
    const u = userEvent.setup()
    renderDialog(['flags:read']) // object-scoped export perm, no roles:read
    await u.click(await screen.findByRole('radio', { name: /flags/i }))

    expect(screen.queryByLabelText('Red-teamer')).not.toBeInTheDocument()
    expect(rolesSpy).not.toHaveBeenCalled()
  })

  it('blocks export while a typed red-teamer term is unresolved (no option picked)', async () => {
    server.use(
      templatesHandler([info('flags', 'Flags')]),
      http.get('http://localhost/api/v1/evaluation-groups/:id/members', () =>
        HttpResponse.json({
          items: [{ user: { id: 'u1', email: 'alice@ex.com' }, roles: [] }],
          total: 1,
          limit: 20,
          offset: 0,
        }),
      ),
    )
    const u = userEvent.setup()
    renderDialog(['flags:read', 'roles:read'])
    await u.click(await screen.findByRole('radio', { name: /flags/i }))
    fireEvent.change(await screen.findByLabelText('Red-teamer'), { target: { value: 'ali' } })

    // Typed but not picked → export blocked + inline hint, so we never silently export unfiltered.
    await waitFor(() => expect(screen.getByText(/pick a red-teamer/i)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^export$/i })).toBeDisabled()
  })

  it('reviews export reviewer picker searches annotators server-side', async () => {
    let searchParam: string | null = null
    server.use(
      templatesHandler([info('reviews', 'Reviews')]),
      http.get('http://localhost/api/v1/evaluation-groups/:id/annotators', ({ request }) => {
        searchParam = new URL(request.url).searchParams.get('search')
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
    )
    const u = userEvent.setup()
    renderDialog(['reviews:read', 'evaluation_groups:manage_members'])
    await u.click(await screen.findByRole('radio', { name: /reviews/i }))

    fireEvent.change(await screen.findByLabelText('Reviewer'), { target: { value: 'rev' } })
    await waitFor(() => expect(searchParam).toBe('rev'))
  })

  it('reviews export queues the picked reviewer as user_id', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      templatesHandler([info('reviews', 'Reviews')]),
      http.get('http://localhost/api/v1/evaluation-groups/:id/annotators', () =>
        HttpResponse.json({
          items: [{ id: 'rev-1', email: 'rev@ex.com' }],
          total: 1,
          limit: 100,
          offset: 0,
        }),
      ),
      http.post('http://localhost/api/v1/exports/jobs', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          {
            id: 'job-3',
            template: 'reviews',
            status: 'pending',
            evaluation_id: null,
            evaluation_group_id: GROUP_ID,
            error: null,
            created_at: '2026-01-01T00:00:00Z',
            expires_at: null,
          },
          { status: 202 },
        )
      }),
    )
    const u = userEvent.setup()
    renderDialog(['reviews:read', 'evaluation_groups:manage_members'])
    await u.click(await screen.findByRole('radio', { name: /reviews/i }))
    await u.click(await screen.findByLabelText('Reviewer')) // open the combobox
    await u.click(await screen.findByRole('option', { name: 'rev@ex.com' }))
    await u.click(screen.getByRole('button', { name: /^export$/i }))

    await waitFor(() => expect(body).not.toBeNull())
    // Regression guard: the reviewer picker writes the shared userId; onExport must serialize it for
    // the reviews template even though showUser is false there.
    expect(body).toMatchObject({ template: 'reviews', filters: { user_id: 'rev-1' } })
  })

  it('hides the reviewer picker when the caller cannot manage group members', async () => {
    // The annotators source is server-gated on evaluation_groups:manage_members; showing the picker
    // to a reviews-export caller without it would only produce a 403 + error toast.
    server.use(templatesHandler([info('reviews', 'Reviews')]))
    const u = userEvent.setup()
    renderDialog(['reviews:read'])
    await u.click(await screen.findByRole('radio', { name: /reviews/i }))

    expect(screen.queryByLabelText('Reviewer')).not.toBeInTheDocument()
  })
})
