import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { queryClient } from '@/lib/query'
import { authWrapper } from '@/lib/auth/auth.testutils'
import type { MeResponse, PublicationStatus } from '@/lib/api/types'
import { EvaluationGroupFormPage } from './evaluation-group-form-page'
import { licenseStub, noLicenseStub } from '@/features/licenses/test-fixtures'

// The data-license picker fires GET /api/v1/licenses on every render; stub a small
// catalog (one default, one override, plus the no-license entry) so the request is handled
// and the picker has options.
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const LICENSES = {
  items: [
    licenseStub('CC-BY-4.0', { name: 'Creative Commons Attribution 4.0', is_default: true }),
    licenseStub('CC0-1.0', { name: 'Creative Commons Zero 1.0', is_default: false }),
    noLicenseStub(),
  ],
  total: 3,
  limit: 100,
  offset: 0,
}

// The model picker fires GET /api/v1/ai-models when the caller has models:read.
const AI_MODELS = {
  items: [
    { id: 'model-1', name: 'GPT-Test', labels: ['audited', 'eu-only', 'self-hosted'] },
    { id: 'model-2', name: 'Claude-Test', labels: [] },
  ],
  total: 2,
  limit: 100,
  offset: 0,
}

beforeEach(() => {
  server.use(
    http.get('http://localhost/api/v1/licenses', () => HttpResponse.json(LICENSES)),
    http.get('http://localhost/api/v1/ai-models', () => HttpResponse.json(AI_MODELS)),
    // Save on a draft edit transitions, so every draft fixture reaches this; suites that assert
    // on the call override it.
    http.post('http://localhost/api/v1/evaluation-groups/:id/submit', ({ params }) =>
      HttpResponse.json({ id: params.id, status: 'pending_approval' }, { status: 200 }),
    ),
  )
})

function renderForm(
  path: string,
  routePath: string,
  Wrapper: ReturnType<typeof authWrapper> = authWrapper([
    'evaluation_groups:create',
    'evaluation_groups:update',
  ]),
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={routePath} element={<EvaluationGroupFormPage />} />
            <Route path="/evaluation-groups" element={<div>Groups list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationGroupFormPage — breadcrumb', () => {
  it('create mode renders a link to /evaluation-groups', async () => {
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderForm('/evaluation-groups/new', '/evaluation-groups/new')

    await waitFor(() =>
      expect(screen.getByRole('link', { name: /evaluation groups/i })).toBeInTheDocument(),
    )
    expect(screen.getByRole('link', { name: /evaluation groups/i })).toHaveAttribute(
      'href',
      '/evaluation-groups',
    )
    expect(screen.getByText('New')).toBeInTheDocument()
  })

  it('edit mode names the group being edited, not just "Edit"', async () => {
    const GROUP_ID = 'grp-0001-0000-0000-000000000000'
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Test group',
          description: 'A test',
          created_by_id: 'user-0001',
          status: 'draft',
          access_level: 'public',
          start_date: '2026-01-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          // In-group edit authority — the form gates on this, not the global permission.
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )

    // Global perms intentionally lack evaluation_groups:update — edit authority comes
    // from the group's user_permissions above (the in-group-owner persona).
    renderForm(
      `/evaluation-groups/${GROUP_ID}/edit`,
      '/evaluation-groups/:id/edit',
      authWrapper(['evaluation_groups:read']),
    )

    await waitFor(() =>
      expect(screen.getByRole('link', { name: /evaluation groups/i })).toBeInTheDocument(),
    )
    // The identity line says which group this form edits; "Edit" alone named nothing.
    const identity = screen.getByRole('navigation', { name: 'Evaluation group' })
    expect(within(identity).getByRole('link', { name: 'Test group' })).toHaveAttribute(
      'href',
      `/evaluation-groups/${GROUP_ID}`,
    )
  })

  it('edit mode shows Not authorized when the caller lacks in-group update', async () => {
    const GROUP_ID = 'grp-0002-0000-0000-000000000000'
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Read-only group',
          description: 'A test',
          created_by_id: 'user-0001',
          status: 'draft',
          access_level: 'public',
          start_date: '2026-01-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          // Member who can see the group but holds no edit authority on it.
          user_permissions: ['evaluation_groups:read'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )

    renderForm(
      `/evaluation-groups/${GROUP_ID}/edit`,
      '/evaluation-groups/:id/edit',
      authWrapper(['evaluation_groups:read']),
    )

    await waitFor(() => expect(screen.getByText(/access to this section/i)).toBeInTheDocument())
    expect(screen.queryByText('Edit')).toBeNull()
  })
})

function LocationProbe() {
  return <div data-testid="loc">{useLocation().pathname}</div>
}

function renderAt(path: string, routePath: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluation_groups:create', 'evaluation_groups:update'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <LocationProbe />
          <Routes>
            <Route path={routePath} element={<EvaluationGroupFormPage />} />
            <Route path="*" element={null} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationGroupFormPage — draft & public confirm', () => {
  it('Save as draft with only a title posts to /draft and lands on the new edit page', async () => {
    server.use(
      http.post('http://localhost/api/v1/evaluation-groups/draft', () =>
        HttpResponse.json(
          {
            id: 'new-draft-id',
            title: 'WIP',
            description: null,
            created_by_id: 'u',
            status: 'draft',
            access_level: 'invitation_only',
            start_date: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
          { status: 201 },
        ),
      ),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'WIP' } })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe('/evaluation-groups/new-draft-id/edit'),
    )
  })

  it('Save as draft is disabled until a title is entered', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    await screen.findByLabelText('Title')
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'x' } })
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeEnabled()
  })

  it('defaults the access level to invitation_only (not public)', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const select = (await screen.findByLabelText('Access level')) as HTMLSelectElement
    expect(select.value).toBe('invitation_only')
  })

  it('Create as public asks for confirmation before submitting', async () => {
    let created = false
    server.use(
      http.post('http://localhost/api/v1/evaluation-groups', () => {
        created = true
        return HttpResponse.json(
          {
            id: 'g1',
            title: 'Pub',
            description: 'd',
            created_by_id: 'u',
            status: 'pending_approval',
            access_level: 'public',
            start_date: '2026-09-01',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
          { status: 201 },
        )
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Pub' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await screen.findByText(/make this group public/i)
    expect(created).toBe(false)

    // findBy, not getBy: the dialog's contents enter the a11y tree only after
    // Modal's showModal() effect runs — a sync query races it (flaky on CI).
    fireEvent.click(await screen.findByRole('button', { name: /save as public/i }))
    await waitFor(() => expect(created).toBe(true))
  })
})

function renderWith(
  path: string,
  routePath: string,
  permissions: string[],
  organization: MeResponse['organization'] = null,
  client?: QueryClient,
) {
  // A bare client has no MutationCache, so a test about what the operator is told would pass
  // against a form that tells them nothing — pass the app's own client for those.
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions, [], organization)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <LocationProbe />
          <Routes>
            <Route path={routePath} element={<EvaluationGroupFormPage />} />
            <Route path="*" element={null} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationGroupFormPage — draft vs non-draft edit', () => {
  const GROUP_ID = 'grp-3312-0000-0000-000000000000'

  function renderEdit(status: PublicationStatus, client?: QueryClient) {
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'A group',
          description: 'd',
          created_by_id: 'u',
          status,
          access_level: 'invitation_only',
          start_date: '2027-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
    )
    renderWith(
      `/evaluation-groups/${GROUP_ID}/edit`,
      '/evaluation-groups/:id/edit',
      ['evaluation_groups:update'],
      null,
      client,
    )
  }

  // Asserting the whole set, in order, rather than probing for one label: that catches a button
  // appearing where it shouldn't as well as one going missing.
  it('a draft edit keeps the partial save — this is where Duplicate lands', async () => {
    renderEdit('draft')
    await screen.findByLabelText('Title')
    expect(screen.getAllByRole('button', { name: /save/i }).map((b) => b.textContent)).toEqual([
      'Save as draft',
      'Save & submit',
    ])
  })

  // A PATCH cannot change the status, so here the button saved without producing a draft.
  it('a published edit offers Save only', async () => {
    renderEdit('published')
    await screen.findByLabelText('Title')
    expect(screen.getAllByRole('button', { name: /save/i }).map((b) => b.textContent)).toEqual([
      'Save',
    ])
  })

  function trackWrites(submitResponse: () => Response) {
    const calls: string[] = []
    server.use(
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () => {
        calls.push('patch')
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/submit`, () => {
        calls.push('submit')
        return submitResponse()
      }),
    )
    return calls
  }

  const submitOk = () =>
    HttpResponse.json({ id: GROUP_ID, status: 'pending_approval' }, { status: 200 })

  it('Save on a draft edit patches the fields and sends the group to review', async () => {
    const calls = trackWrites(submitOk)
    renderEdit('draft')
    await screen.findByLabelText('Title')
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe(`/evaluation-groups/${GROUP_ID}`),
    )
    expect(calls).toEqual(['patch', 'submit'])
  })

  it('Save on a published edit patches only — no transition', async () => {
    const calls = trackWrites(submitOk)
    renderEdit('published')
    await screen.findByLabelText('Title')
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe(`/evaluation-groups/${GROUP_ID}`),
    )
    expect(calls).toEqual(['patch'])
  })

  // A scenario gap is the refusal the form cannot pre-empt — it cannot see the group's
  // evaluations. The fields are saved by the time it lands, so the operator has to be told on the
  // form: rendered through the app's own client, since a bare one has no MutationCache and this
  // assertion would pass against a form that says nothing.
  it('a refused submit is reported on the form, once, with the edit already saved', async () => {
    const calls = trackWrites(() =>
      HttpResponse.json(
        {
          title: 'Bad Request',
          status: 400,
          detail:
            'Cannot submit an incomplete group: Every evaluation needs at least one scenario; add one to: Bootstrap eval.',
        },
        { status: 400 },
      ),
    )
    renderEdit('draft', queryClient)
    await screen.findByLabelText('Title')
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    await waitFor(() => expect(calls).toEqual(['patch', 'submit']))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('were saved, but the group was not submitted')
    expect(alert).toHaveTextContent('Bootstrap eval')
    // One failure, one channel — the form renders it, so the global toast must stay quiet.
    expect(toast.error).not.toHaveBeenCalled()
    expect(screen.getByTestId('loc').textContent).toBe(`/evaluation-groups/${GROUP_ID}/edit`)
    queryClient.clear()
  })

  // The gate refuses a start date before today, and the form owns that field — so it is caught
  // before anything is written, rather than after a PATCH that then has to be explained.
  it('a past start date blocks the submit without writing anything', async () => {
    const calls = trackWrites(submitOk)
    renderEdit('draft')
    const startInput = (await screen.findByLabelText('Start date')) as HTMLInputElement
    await waitFor(() => expect(startInput.value).toBe('2027-05-01'))
    fireEvent.change(startInput, { target: { value: '2020-01-01' } })
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    expect(await screen.findByText(/today or later to submit/i)).toBeInTheDocument()
    expect(calls).toEqual([])
  })

  // The pair's whole point: one keeps you in draft, the other sends you to review. Without this
  // the invariant survives a Save-as-draft that also submits.
  it('Save as draft on a draft edit never submits', async () => {
    const calls = trackWrites(submitOk)
    renderEdit('draft')
    await screen.findByLabelText('Title')
    fireEvent.click(screen.getByRole('button', { name: /^save as draft$/i }))

    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe(`/evaluation-groups/${GROUP_ID}`),
    )
    expect(calls).toEqual(['patch'])
  })
})

describe('EvaluationGroupFormPage — clearing & org picker', () => {
  const GROUP_ID = 'grp-2222-0000-0000-000000000000'

  it('Save as draft on an edit sends explicit null for cleared start_date and description', async () => {
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Draft',
          description: 'old description',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          start_date: '2026-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const startInput = (await screen.findByLabelText('Start date')) as HTMLInputElement
    await waitFor(() => expect(startInput.value).toBe('2026-05-01'))
    fireEvent.change(startInput, { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!.start_date).toBeNull()
    expect(patchBody!.description).toBeNull()
  })

  // The create-side half of the same guarantee: on a POST the empty optionals travel as `null`
  // rather than being dropped from the body.
  it('Save as draft with only a title sends explicit null for the empty optional fields', async () => {
    let draftBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups/draft', async ({ request }) => {
        draftBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g-draft', status: 'draft' }, { status: 201 })
      }),
    )
    renderWith('/evaluation-groups/new', '/evaluation-groups/new', ['evaluation_groups:create'])

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Just a title' } })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() => expect(draftBody).toBeDefined())
    expect(draftBody!.description).toBeNull()
    expect(draftBody!.start_date).toBeNull()
    expect(draftBody!.end_date).toBeNull()
    expect(draftBody!.organization_id).toBeNull()
  })

  it('organization access shows the picker and Create sends organization_id (admin sees all orgs)', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({
          items: [
            { id: 'org-a', name: 'Acme' },
            { id: 'org-b', name: 'Globex' },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderWith('/evaluation-groups/new', '/evaluation-groups/new', [
      'evaluation_groups:create',
      'evaluation_groups:manage',
      'organizations:read',
    ])
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Org grp' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'organization' } })

    const orgSelect = (await screen.findByLabelText('Organization')) as HTMLSelectElement
    await waitFor(() => expect(screen.getByRole('option', { name: 'Globex' })).toBeInTheDocument())
    fireEvent.change(orgSelect, { target: { value: 'org-b' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.access_level).toBe('organization')
    expect(postBody!.organization_id).toBe('org-b')
  })
})

describe('EvaluationGroupFormPage — organization picker', () => {
  const twoOrgs = () =>
    http.get('http://localhost/api/v1/organizations', () =>
      HttpResponse.json({
        items: [
          { id: 'org-1', name: 'Acme Corp' },
          { id: 'org-2', name: 'Globex' },
        ],
        total: 2,
        limit: 100,
        offset: 0,
      }),
    )

  it('lists every organization for a manager', async () => {
    server.use(twoOrgs())

    renderForm(
      '/evaluation-groups/new',
      '/evaluation-groups/new',
      authWrapper(['evaluation_groups:create', 'evaluation_groups:manage', 'organizations:read']),
    )
    fireEvent.change(await screen.findByLabelText('Access level'), {
      target: { value: 'organization' },
    })

    expect(await screen.findByRole('option', { name: 'Acme Corp' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Globex' })).toBeInTheDocument()
  })

  it('restricts a non-admin to their own organization', async () => {
    server.use(twoOrgs())

    renderForm(
      '/evaluation-groups/new',
      '/evaluation-groups/new',
      authWrapper(['evaluation_groups:create', 'organizations:read'], [], {
        id: 'org-1',
        name: 'Acme Corp',
      }),
    )
    fireEvent.change(await screen.findByLabelText('Access level'), {
      target: { value: 'organization' },
    })

    expect(await screen.findByRole('option', { name: 'Acme Corp' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Globex' })).not.toBeInTheDocument()
  })
})

describe('EvaluationGroupFormPage — analytics visibility', () => {
  const GROUP_ID = 'grp-3333-0000-0000-000000000000'

  it('defaults both metrics-access selects to members_personal_metrics', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const during = (await screen.findByLabelText('While active')) as HTMLSelectElement
    const after = screen.getByLabelText('After finish') as HTMLSelectElement
    expect(during.value).toBe('members_personal_metrics')
    expect(after.value).toBe('members_personal_metrics')
  })

  it('choosing public access sets both metrics selects to inherit_group_access', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const during = (await screen.findByLabelText('While active')) as HTMLSelectElement
    const after = screen.getByLabelText('After finish') as HTMLSelectElement
    expect(during.value).toBe('members_personal_metrics') // default before switching
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })
    expect(during.value).toBe('inherit_group_access')
    expect(after.value).toBe('inherit_group_access')
  })

  it('Create sends the chosen metrics-access levels', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Metrics grp' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('While active'), { target: { value: 'all_members' } })
    fireEvent.change(screen.getByLabelText('After finish'), {
      target: { value: 'inherit_group_access' },
    })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.metrics_access_during).toBe('all_members')
    expect(postBody!.metrics_access_after).toBe('inherit_group_access')
  })

  // Guards `draftBody`'s own metrics lines, which the Save path above does not reach: without
  // this, dropping them from the helper resets a group's metrics visibility to the backend
  // defaults on every Save as draft, silently.
  it('Save as draft on an edit persists a changed metrics-access level', async () => {
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Draft',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          metrics_access_during: 'owner_only',
          metrics_access_after: 'owner_only',
          start_date: '2027-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const during = (await screen.findByLabelText('While active')) as HTMLSelectElement
    await waitFor(() => expect(during.value).toBe('owner_only'))
    fireEvent.change(during, { target: { value: 'members_personal_metrics' } })
    fireEvent.click(screen.getByRole('button', { name: /^save as draft$/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!.metrics_access_during).toBe('members_personal_metrics')
    expect(patchBody!.metrics_access_after).toBe('owner_only')
  })

  it('Save on an edit persists a changed metrics-access level', async () => {
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Draft',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          metrics_access_during: 'owner_only',
          metrics_access_after: 'owner_only',
          start_date: '2027-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const during = (await screen.findByLabelText('While active')) as HTMLSelectElement
    await waitFor(() => expect(during.value).toBe('owner_only'))
    fireEvent.change(during, { target: { value: 'members_personal_metrics' } })
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!.metrics_access_during).toBe('members_personal_metrics')
    expect(patchBody!.metrics_access_after).toBe('owner_only')
  })

  it('edit mode prefills both selects from the loaded group', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Configured',
          description: 'd',
          created_by_id: 'u',
          status: 'published',
          access_level: 'public',
          metrics_access_during: 'all_members',
          metrics_access_after: 'inherit_group_access',
          start_date: '2026-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const during = (await screen.findByLabelText('While active')) as HTMLSelectElement
    await waitFor(() => expect(during.value).toBe('all_members'))
    expect((screen.getByLabelText('After finish') as HTMLSelectElement).value).toBe(
      'inherit_group_access',
    )
  })
})

describe('EvaluationGroupFormPage — data license', () => {
  it('lists the catalog and Create submits the chosen data_license', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Licensed grp' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    // The picker lists the catalog names plus the "Platform default" sentinel.
    expect(
      await screen.findByRole('option', { name: /creative commons zero 1\.0/i }),
    ).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Data license'), {
      target: { value: licenseStub('CC0-1.0').id },
    })
    // Default access is invitation_only, so no public-confirm dialog gates the submit.
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.data_license_id).toBe(licenseStub('CC0-1.0').id)
  })

  it('a private create sends the no-license id (the access-level default)', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Private grp' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    await waitFor(() =>
      expect((screen.getByLabelText('Data license') as HTMLSelectElement).value).toBe(
        noLicenseStub().id,
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.data_license_id).toBe(noLicenseStub().id)
  })

  it('omits the licence when the catalog never resolved, so the backend derives it', async () => {
    // The degraded path: with `GET /licenses` failing (or slower than the operator), the console
    // cannot know the no-licence row. Sending `null` here would read as a deliberate "inherit" and
    // license a private engagement under the platform default without saying so.
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Degraded' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect('data_license_id' in postBody!).toBe(false)
  })

  it('says the licence list failed instead of leaving the field reading "Platform default"', async () => {
    // The wire body above is only half the story: the select shows the inherit option (the only one
    // left) while the server derives "No license" from the access level. Both the seed and the
    // mismatch note are suppressed by the same missing sentinel, so without this note the form
    // silently disagrees with what it saves.
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    const note = await screen.findByText(/license list could not be loaded/i)
    expect(note).toHaveTextContent(/private engagements get no license/i)
    // Wired to the control, not just sitting under it.
    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    expect(select.getAttribute('aria-describedby')).toContain(note.id)
    // And it is the degraded note, not the mismatch note (which cannot be computed without the row).
    expect(screen.queryByText(/usually carry no data license/i)).toBeNull()
  })

  it('does not blame the request when the list loaded but has no sentinel on it', async () => {
    // Two different ways of lacking the sentinel: the request failed, or it succeeded and the entry
    // is not on this page (one page of 100, ordered by name). Keying the note on "no sentinel" alone
    // told the operator the list "could not be loaded" about a list they can see, and on the edit
    // arm advised a reload that would change nothing.
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({
          items: [licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })],
          total: 101,
          limit: 100,
          offset: 0,
        }),
      ),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    // The consequence still has to be stated — the server will derive the licence — but not as a
    // failed fetch.
    const note = await screen.findByText(/does not include the .No license. entry/i)
    expect(note).toHaveTextContent(/private engagements get no license/i)
    expect(screen.queryByText(/could not be loaded/i)).toBeNull()
  })

  it('names the default for the selected access level, not always the private one', async () => {
    // The note states what the server will derive, and it derives per access level — so on `public`
    // the private rule is the note being wrong in the one state it exists for.
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Access level'), { target: { value: 'public' } })

    const note = await screen.findByText(/license list could not be loaded/i)
    expect(note).toHaveTextContent(/the platform default license/i)
    expect(note).not.toHaveTextContent(/get no license/i)
  })

  it('stops the empty option claiming "Platform default" when the sentinel is unknown', async () => {
    // With the field omitted the server derives from the access level, so the option left selected
    // must not name the one licence a private engagement will not get — the note underneath said
    // "no license" while the control read "Platform default".
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    await screen.findByText(/license list could not be loaded/i)
    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    expect(select.options).toHaveLength(1)
    expect(select.options[0]).toHaveTextContent('Default for this access level')
  })

  it('a private draft saved with the catalog unavailable omits the licence too', async () => {
    // The create path has its own test above; without this one the third argument at the draft call
    // site is free — `draftBody`'s parameter defaults to "send null", so dropping it would license a
    // private engagement under the platform default exactly when the console knows least.
    let draftBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups/draft', async ({ request }) => {
        draftBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g-draft', status: 'draft' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Degraded draft' } })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() => expect(draftBody).toBeDefined())
    expect('data_license_id' in draftBody!).toBe(false)
  })

  it('changing the access level on an edit leaves the stored licence alone', async () => {
    // Derivation is a create-time affordance. On an existing group the licence stays put — its
    // delivered exports already name it — and the mismatch note is what speaks instead.
    const GROUP_ID = 'grp-lic0-0000-0000-000000000000'
    const sentinel = noLicenseStub()
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({
          items: [licenseStub('CC-BY-4.0'), sentinel],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Private engagement',
          description: 'd',
          created_by_id: 'user-0001',
          status: 'approved',
          access_level: 'invitation_only',
          start_date: '2026-01-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          data_license_id: sentinel.id,
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
    )
    renderAt(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit')

    const licence = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(licence.value).toBe(sentinel.id))
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })

    expect(licence.value).toBe(sentinel.id)
    expect(
      screen.getByText(/will carry no data license, so its data is not shareable/i),
    ).toBeInTheDocument()
  })

  it('a public create sends null (inherit the platform default)', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Public grp' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    await waitFor(() =>
      expect((screen.getByLabelText('Data license') as HTMLSelectElement).value).toBe(
        noLicenseStub().id,
      ),
    )
    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))
    fireEvent.click(await screen.findByRole('button', { name: /save as public/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.data_license_id).toBeNull()
  })

  it('switching back to invitation_only re-selects no license', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(noLicenseStub().id))

    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })
    expect(picker.value).toBe('')

    fireEvent.change(screen.getByLabelText('Access level'), {
      target: { value: 'invitation_only' },
    })
    expect(picker.value).toBe(noLicenseStub().id)
  })

  it('keeps an explicitly chosen license when the access level changes', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(noLicenseStub().id))
    fireEvent.change(picker, { target: { value: licenseStub('CC0-1.0').id } })

    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })

    expect(picker.value).toBe(licenseStub('CC0-1.0').id)
  })

  it('hints when a private group carries a license', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(noLicenseStub().id))
    expect(screen.queryByText(/usually carry no data license/i)).not.toBeInTheDocument()

    fireEvent.change(picker, { target: { value: licenseStub('CC0-1.0').id } })

    expect(screen.getByText(/usually carry no data license/i)).toBeInTheDocument()
  })

  it('associates the mismatch hint with the picker, not just places it below', async () => {
    // A note that only sits next to the control is invisible to a screen reader; the picker folds
    // this id into the select's `aria-describedby`.
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(noLicenseStub().id))
    fireEvent.change(picker, { target: { value: licenseStub('CC0-1.0').id } })

    const hint = screen.getByText(/usually carry no data license/i)
    expect(hint.id).toBeTruthy()
    expect(picker.getAttribute('aria-describedby')?.split(' ')).toContain(hint.id)
  })

  it('hints when a non-private group carries no license', async () => {
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')
    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(noLicenseStub().id))

    fireEvent.change(screen.getByLabelText('Access level'), { target: { value: 'public' } })
    fireEvent.change(picker, { target: { value: noLicenseStub().id } })

    expect(screen.getByText(/will carry no data license/i)).toBeInTheDocument()
  })

  it('edit prefills the group license and Save sends the changed value', async () => {
    const GROUP_ID = 'grp-lic-0000-0000-000000000000'
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Licensed',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          start_date: '2027-05-01',
          data_license_id: licenseStub('CC0-1.0').id,
          effective_license: licenseStub('CC0-1.0'),
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(licenseStub('CC0-1.0').id))
    fireEvent.change(picker, { target: { value: licenseStub('CC-BY-4.0').id } })
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!.data_license_id).toBe(licenseStub('CC-BY-4.0').id)
  })

  it('edit resetting the license to Platform default PATCHes data_license: null', async () => {
    const GROUP_ID = 'grp-lic-reset-0000-000000000000'
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Licensed',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          start_date: '2027-05-01',
          data_license_id: licenseStub('CC0-1.0').id,
          effective_license: licenseStub('CC0-1.0'),
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: GROUP_ID, status: 'draft' }, { status: 200 })
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    const picker = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe(licenseStub('CC0-1.0').id))
    // Selecting the empty "Platform default" option clears the override.
    fireEvent.change(picker, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /^save & submit$/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!.data_license_id).toBeNull()
  })

  it('Save as draft carries the chosen data_license', async () => {
    let draftBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups/draft', async ({ request }) => {
        draftBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'd1', status: 'draft' }, { status: 201 })
      }),
    )
    renderAt('/evaluation-groups/new', '/evaluation-groups/new')

    fireEvent.change(await screen.findByLabelText('Title'), {
      target: { value: 'Draft w/ license' },
    })
    fireEvent.change(screen.getByLabelText('Data license'), {
      target: { value: licenseStub('CC0-1.0').id },
    })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() => expect(draftBody).toBeDefined())
    expect(draftBody!.data_license_id).toBe(licenseStub('CC0-1.0').id)
  })
})

describe('EvaluationGroupFormPage — model subset', () => {
  it('Create requires at least one model when the caller can read models', async () => {
    let created = false
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', () => {
        created = true
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderWith('/evaluation-groups/new', '/evaluation-groups/new', [
      'evaluation_groups:create',
      'models:read',
    ])
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'No models' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    // The picker is present but nothing is selected.
    await screen.findByLabelText('Allowed models')
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    expect(await screen.findByText(/select at least one model/i)).toBeInTheDocument()
    expect(created).toBe(false)
  })

  it('Create sends the selected allowed_model_ids', async () => {
    let postBody: Record<string, unknown> | undefined
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.post('http://localhost/api/v1/evaluation-groups', async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'g1', status: 'pending_approval' }, { status: 201 })
      }),
    )
    renderWith('/evaluation-groups/new', '/evaluation-groups/new', [
      'evaluation_groups:create',
      'models:read',
    ])
    const u = userEvent.setup()
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'With models' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'd' } })
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } })
    // Open the multiselect and pick a model.
    await u.click(screen.getByLabelText('Allowed models'))
    await u.click(await screen.findByRole('option', { name: /GPT-Test/ }))
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => expect(postBody).toBeDefined())
    expect(postBody!.allowed_model_ids).toEqual(['model-1'])
  })

  it("shows a model's labels while picking it for the group", async () => {
    server.use(
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderWith('/evaluation-groups/new', '/evaluation-groups/new', [
      'evaluation_groups:create',
      'models:read',
    ])
    const u = userEvent.setup()

    await u.click(await screen.findByLabelText('Allowed models'))

    // The labels join the option's accessible name, so they are read out with it — summarised past
    // two, because the full set is unbounded and a model carrying none adds nothing. The API sorts
    // them, so the two that survive are alphabetical: the rest has to stay reachable on hover.
    expect(
      await screen.findByRole('option', { name: 'GPT-Test audited, eu-only +1' }),
    ).toBeInTheDocument()
    expect(screen.getByText('audited, eu-only +1')).toHaveAttribute(
      'title',
      'audited, eu-only, self-hosted',
    )
    expect(screen.getByRole('option', { name: 'Claude-Test' })).toBeInTheDocument()
  })

  it('edit prefills the picker from the group allowed_models', async () => {
    const GROUP_ID = 'grp-models-0000-0000-000000000000'
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Configured',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          start_date: '2026-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          allowed_models: [
            {
              id: 'sub-1',
              evaluation_group_id: GROUP_ID,
              model_id: 'model-2',
              name: 'Claude-Test',
            },
          ],
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
      'models:read',
    ])

    // The prefilled model shows as a removable chip; the listbox is closed, so the
    // unselected model's name is not rendered.
    expect(await screen.findByRole('button', { name: /remove claude-test/i })).toBeInTheDocument()
    expect(screen.queryByText('GPT-Test')).toBeNull()
  })

  it('edit shows the picker for an in-group owner without the global models:read', async () => {
    const GROUP_ID = 'grp-ingroup-0000-0000-000000000000'
    let modelsUrl: URL | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Owned',
          description: 'd',
          created_by_id: 'u',
          status: 'draft',
          access_level: 'invitation_only',
          start_date: '2026-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          allowed_models: [
            { id: 'sub-1', evaluation_group_id: GROUP_ID, model_id: 'model-1', name: 'GPT-Test' },
          ],
          // Object-scoped authority — the in-group owner holds models:read here.
          user_permissions: ['evaluation_groups:update', 'models:read'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/ai-models', ({ request }) => {
        modelsUrl = new URL(request.url)
        return HttpResponse.json(AI_MODELS)
      }),
    )
    // Global perms intentionally lack models:read — authority is the group's user_permissions.
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    expect(await screen.findByLabelText('Allowed models')).toBeInTheDocument()
    // The registry call authorizes via `for_group` (no global permission).
    await waitFor(() => expect(modelsUrl).toBeDefined())
    expect(modelsUrl!.searchParams.get('for_group')).toBe(GROUP_ID)
  })

  it('masked editor without models:read PATCHes without allowed_model_ids', async () => {
    // No picker for a masked editor; saving must leave the (masked) subset untouched —
    // the PATCH body must omit the key, never send [] (which would wipe the subset).
    const GROUP_ID = 'grp-masked0-0000-0000-000000000000'
    let patchBody: Record<string, unknown> | undefined
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Masked',
          description: 'd',
          created_by_id: 'u',
          status: 'approved',
          access_level: 'invitation_only',
          start_date: '2026-05-01',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
          metrics_access_during: 'owner_only',
          metrics_access_after: 'owner_only',
          allowed_models: null, // masked — the caller lacks models:read
          user_permissions: ['evaluation_groups:update'],
        }),
      ),
      http.get('http://localhost/api/v1/organizations', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.patch(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          {
            id: GROUP_ID,
            title: 'Masked',
            description: 'edited',
            created_by_id: 'u',
            status: 'approved',
            access_level: 'invitation_only',
            start_date: '2026-05-01',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
          { status: 200 },
        )
      }),
    )
    renderWith(`/evaluation-groups/${GROUP_ID}/edit`, '/evaluation-groups/:id/edit', [
      'evaluation_groups:update',
    ])

    await screen.findByLabelText('Title')
    expect(screen.queryByLabelText('Allowed models')).toBeNull()
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'edited' } })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(patchBody).toBeDefined())
    expect(patchBody!).not.toHaveProperty('allowed_model_ids')
  })
})
