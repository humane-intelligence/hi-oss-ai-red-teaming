import { describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { EvaluationFormPage } from './evaluation-form-page'
import { licenseStub, noLicenseStub } from '@/features/licenses/test-fixtures'

const GROUP_ID = 'grp-eval-0000-0000-000000000000'

function groupResponse(userPermissions: string[], extra: Record<string, unknown> = {}) {
  return {
    id: GROUP_ID,
    title: 'Host group',
    description: 'A group',
    created_by_id: 'user-0001',
    // Must be a status that accepts evaluations — the create form refuses to
    // render for a pre-approval group (the API would 409 the submit).
    status: 'approved',
    access_level: 'public',
    start_date: '2026-01-01',
    data_license_id: null,
    effective_license: licenseStub('CC-BY-4.0'),
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    evaluations: [],
    user_permissions: userPermissions,
    ...extra,
  }
}

function handlers(userPermissions: string[]) {
  return [
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
      HttpResponse.json(groupResponse(userPermissions)),
    ),
    http.get('http://localhost/api/v1/evaluation-groups', () =>
      HttpResponse.json({
        items: [groupResponse(userPermissions)],
        total: 1,
        limit: 100,
        offset: 0,
      }),
    ),
    http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
  ]
}

// Renders the create form at /evaluations/new?group=<id>. The form gates on the
// parent group's user_permissions (object authority), not the global set.
function renderCreate(entry = `/evaluations/new?group=${GROUP_ID}`) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluations:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/evaluations/new" element={<EvaluationFormPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

// The cross-group picker path: no preselected group, the form offers a <select>
// of groups that can take a new evaluation.
function renderCreateNoGroup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluations:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/evaluations/new']}>
          <Routes>
            <Route path="/evaluations/new" element={<EvaluationFormPage />} />
            <Route path="/evaluation-groups" element={<div>Groups list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

const EVAL_ID = 'eval-0000-0000-0000-000000000000'

// The edit path learns its group from the evaluation, not from the query string.
function renderEdit() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluations:read', 'evaluations:update'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/evaluations/${EVAL_ID}/edit`]}>
          <Routes>
            <Route path="/evaluations/:id/edit" element={<EvaluationFormPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationFormPage — the trail on the edit path', () => {
  it('names the group being edited inside, and the evaluation, which the title does not', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({
          id: EVAL_ID,
          title: 'Prompt injection sweep',
          description: null,
          status: 'draft',
          evaluation_group_id: GROUP_ID,
          created_by_id: 'user-0001',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          user_permissions: ['evaluations:update'],
        }),
      ),
      ...handlers(['evaluations:update']),
    )
    renderEdit()

    const identity = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(identity).findByRole('link', { name: 'Host group' })).toBeInTheDocument()

    // Here the path trail earns its place: it carries the evaluation being edited, which neither
    // the identity line above nor the "Edit evaluation" title beside it says.
    const path = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(
      await within(path).findByRole('link', { name: 'Prompt injection sweep' }),
    ).toHaveAttribute('href', `/evaluations/${EVAL_ID}`)
    // The page you are on is the leaf, and a leaf does not link.
    expect(within(path).getByText('Edit')).toBeInTheDocument()
    expect(within(path).queryByRole('link', { name: 'Edit' })).toBeNull()
  })
})

describe('EvaluationFormPage — group picker lifecycle filter', () => {
  it('requests only accepting groups for the picker (server-side filter)', async () => {
    // The lifecycle allowlist lives backend-side: the picker asks the API for
    // groups that accept evaluations instead of re-encoding which statuses do.
    let acceptsParam: string | null = null
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
        acceptsParam = new URL(request.url).searchParams.get('accepts_evaluations')
        return HttpResponse.json({
          items: [
            groupResponse([], { id: 'g-approved', title: 'Approved group', status: 'approved' }),
            groupResponse([], { id: 'g-published', title: 'Published group', status: 'published' }),
          ],
          total: 2,
          limit: 100,
          offset: 0,
        })
      }),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreateNoGroup()

    expect(await screen.findByRole('option', { name: 'Approved group' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Published group' })).toBeInTheDocument()
    expect(acceptsParam).toBe('true')
  })

  it('with groups but none accepting — points at the approval step, not group creation', async () => {
    // The filtered picker query comes back empty; the unfiltered limit-1 probe
    // shows groups exist, so the empty state points at approval.
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
        const url = new URL(request.url)
        if (url.searchParams.get('accepts_evaluations') === 'true')
          return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
        return HttpResponse.json({
          items: [groupResponse([], { id: 'g-draft', title: 'Draft group', status: 'draft' })],
          total: 1,
          limit: 1,
          offset: 0,
        })
      }),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreateNoGroup()

    await waitFor(() =>
      expect(screen.getByText(/submit a group for approval first/i)).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /view groups/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create a group/i })).toBeNull()
  })

  it('with no groups at all — keeps the create-a-group empty state', async () => {
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreateNoGroup()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /create a group/i })).toBeInTheDocument(),
    )
  })

  it('with a failed probe — surfaces the error, not a misleading empty state', async () => {
    // Filtered picker is empty but the unfiltered probe errors, so we can't tell
    // "no groups" from "none accepting" — show the error rather than defaulting to
    // "create a group", which would mislead a viewer whose groups all exist.
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
        if (new URL(request.url).searchParams.get('accepts_evaluations') === 'true')
          return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
        return new HttpResponse(null, { status: 500 })
      }),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreateNoGroup()

    await waitFor(() =>
      expect(screen.getByText(/something went wrong on the server/i)).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /create a group/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /view groups/i })).toBeNull()
  })
})

describe('EvaluationFormPage — create object-gating', () => {
  it('renders the form when the parent group grants in-group evaluations:create', async () => {
    server.use(...handlers(['evaluations:create']))
    renderCreate()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument())
  })

  it('shows Not authorized when the parent group does not grant evaluations:create', async () => {
    // Member who can see the group but holds no create authority on it.
    server.use(...handlers(['evaluation_groups:read']))
    renderCreate()

    await waitFor(() => expect(screen.getByText(/access to this section/i)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Create' })).toBeNull()
  })

  it("the inherit option reflects the parent group's effective license", async () => {
    // An un-overridden evaluation inherits the group's license, not a bare platform
    // default — the picker must say so, else the group license looks like it does nothing.
    const group = groupResponse(['evaluations:create'], {
      data_license_id: licenseStub('CC0-1.0').id,
      effective_license: licenseStub('CC0-1.0'),
    })
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({
          items: [
            licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }),
            licenseStub('CC0-1.0', { name: 'CC0 1.0' }),
          ],
        }),
      ),
    )
    renderCreate()

    expect(await screen.findByRole('option', { name: /inherit \(CC0-1\.0\)/i })).toBeInTheDocument()
  })

  it("warns when the override would make a closed engagement's data shareable", async () => {
    // The write is accepted either way — `validate_license_ref` passes the sentinel like any other
    // live row — so the only thing standing between "the client asked for no licence" and a
    // shareable evaluation is this sentence. The group form has the same pair one level up.
    const group = groupResponse(['evaluations:create'], { status: 'approved' })
    group.effective_license = noLicenseStub()
    const ccby = licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ items: [ccby, noLicenseStub()] }),
      ),
    )
    renderCreate()

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBeGreaterThan(2))
    expect(screen.queryByText(/makes this evaluation's data shareable/i)).toBeNull()

    fireEvent.change(select, { target: { value: ccby.id } })

    const note = await screen.findByText(/makes this evaluation's data shareable/i)
    expect(select.getAttribute('aria-describedby')).toContain(note.id)
  })

  it('says nothing about a mismatch while either licence is still unknown', async () => {
    // The note needs two facts: which row is the sentinel, and what the engagement carries. With the
    // catalog unavailable, treating "not found in the catalog" as "not the sentinel" made the first
    // arm announce a shareable override on an evaluation that carries no licence either — the note
    // asserting the opposite of the truth. Driven through the edit path, because that is where a
    // stored override lives in form state while the catalog that names it is gone.
    const group = groupResponse(['evaluations:update'], { status: 'approved' })
    group.effective_license = noLicenseStub()
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({
          id: EVAL_ID,
          title: 'Closed sweep',
          description: null,
          status: 'draft',
          evaluation_group_id: GROUP_ID,
          data_license_id: noLicenseStub().id,
          created_by_id: 'user-0001',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          user_permissions: ['evaluations:update'],
        }),
      ),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderEdit()

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    // The stored id survives in form state and the picker admits it cannot name it.
    await waitFor(() => expect(select.value).toBe(noLicenseStub().id))
    expect(select.options[select.selectedIndex]?.text).toMatch(/not listed/i)
    expect(screen.queryByText(/shareable/i)).toBeNull()
    expect(screen.queryByText(/unlike the rest of the engagement/i)).toBeNull()
  })

  it('warns when the evaluation alone would carry no license', async () => {
    const group = groupResponse(['evaluations:create'], { status: 'approved' })
    const sentinel = noLicenseStub()
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({
          items: [licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), sentinel],
        }),
      ),
    )
    renderCreate()

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBeGreaterThan(2))
    fireEvent.change(select, { target: { value: sentinel.id } })

    expect(await screen.findByText(/unlike the rest of the engagement/i)).toBeInTheDocument()
  })

  it('offers the no-license entry, so an evaluation under a private group can carry none', async () => {
    const group = groupResponse(['evaluations:create'], { status: 'approved' })
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({
          items: [
            licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }),
            noLicenseStub(),
          ],
        }),
      ),
    )
    renderCreate()

    expect(await screen.findByRole('option', { name: 'No license' })).toBeInTheDocument()
  })

  it('explains the lifecycle gate for a preselected non-approved group', async () => {
    // Authority is present; the group's state is the blocker. A doomed form would
    // only 409 on submit, so the page says why and offers the way back.
    const group = groupResponse(['evaluations:create'], { status: 'draft' })
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(group),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({ items: [group], total: 1, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreate()

    await waitFor(() =>
      expect(screen.getByText(/added once the group is approved/i)).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: 'Create' })).toBeNull()
    expect(screen.getByRole('button', { name: /back to group/i })).toBeInTheDocument()
  })

  it('breadcrumb reflects the parent group when entered from a group', async () => {
    server.use(...handlers(['evaluations:create']))
    renderCreate()

    // The identity line is what names the parent group now.
    const identity = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(await within(identity).findByRole('link', { name: 'Host group' })).toHaveAttribute(
      'href',
      `/evaluation-groups/${GROUP_ID}`,
    )
    expect(within(identity).getByRole('link', { name: 'Evaluation Groups' })).toBeInTheDocument()

    // No path trail on the create flow: it would carry the group the line above ends with, and
    // "New", which is the page title.
    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'New evaluation' })).toBeInTheDocument()
  })

  it('locks the group select when entered from a group — preselected, not editable', async () => {
    server.use(...handlers(['evaluations:create']))
    renderCreate()

    const group = await screen.findByLabelText('Group')
    expect(group).toBeDisabled()
    await waitFor(() => expect(group).toHaveValue(GROUP_ID))
    expect(group).toHaveAccessibleDescription(/adding to this group/i)
  })

  it('shows the preset group even before the groups list resolves', async () => {
    // The disabled select must reflect the parent group without waiting on the (possibly
    // slow) groups list — otherwise it renders stuck on the empty placeholder.
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(groupResponse(['evaluations:create'])),
      ),
      http.get('http://localhost/api/v1/evaluation-groups', async () => {
        await delay('infinite')
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
      http.get('http://localhost/api/v1/licenses', () => HttpResponse.json({ items: [] })),
    )
    renderCreate()

    const group = await screen.findByLabelText('Group')
    expect(group).toBeDisabled()
    await waitFor(() => expect(group).toHaveValue(GROUP_ID))
    expect(screen.getByRole('option', { name: 'Host group' })).toBeInTheDocument()
  })

  it('keeps the group select editable on the standalone create path', async () => {
    server.use(...handlers(['evaluations:create']))
    renderCreate('/evaluations/new')

    expect(await screen.findByLabelText('Group')).toBeEnabled()
    expect(screen.getByRole('link', { name: 'Evaluations' })).toBeInTheDocument()
    expect(screen.queryByText(/adding to this group/i)).toBeNull()
  })

  it('creates with a title only — description is optional and sent as null', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      ...handlers(['evaluations:create']),
      http.post('http://localhost/api/v1/evaluations', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'new-eval' }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderCreate()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument())
    await user.type(screen.getByLabelText('Title'), 'Title only eval')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.title).toBe('Title only eval')
    expect(body!.description).toBeNull()
    expect(body!.evaluation_group_id).toBe(GROUP_ID)
  })
})

const COVER_KEY = '2026/11/09/cover.png'

function uploadHandler(onUpload: () => void = () => {}) {
  return http.post('http://localhost/api/v1/images', () => {
    onUpload()
    return HttpResponse.json(
      {
        id: 'a0000000-0000-0000-0000-000000000001',
        key: COVER_KEY,
        url: `/api/v1/images/${COVER_KEY}`,
        content_type: 'image/png',
        width: 10,
        height: 10,
        size_bytes: 68,
        is_private: false,
        created_at: '2026-11-09T00:00:00Z',
      },
      { status: 201 },
    )
  })
}

function editWithCover(coverImage: string | null) {
  return http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
    HttpResponse.json({
      id: EVAL_ID,
      title: 'Prompt injection sweep',
      description: null,
      status: 'draft',
      evaluation_group_id: GROUP_ID,
      cover_image: coverImage,
      mask_models_enabled: true,
      tags_enabled: true,
      created_by_id: 'user-0001',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      user_permissions: ['evaluations:update'],
    }),
  )
}

function captureUpdate(onBody: (body: Record<string, unknown>) => void) {
  return http.patch(`http://localhost/api/v1/evaluations/${EVAL_ID}`, async ({ request }) => {
    onBody((await request.json()) as Record<string, unknown>)
    return HttpResponse.json({ id: EVAL_ID })
  })
}

const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement

describe('EvaluationFormPage — cover image', () => {
  it('uploads the picked image as public and creates with its key', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      ...handlers(['evaluations:create']),
      uploadHandler(),
      http.post('http://localhost/api/v1/evaluations', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: 'new-eval' }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderCreate()

    await user.type(await screen.findByLabelText('Title'), 'With a cover')
    await user.upload(fileInput(), new File(['x'], 'cover.png', { type: 'image/png' }))
    expect(await screen.findByRole('img', { name: 'Cover image' })).toHaveAttribute(
      'src',
      `http://localhost/api/v1/images/${COVER_KEY}`,
    )
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.cover_image).toBe(COVER_KEY)
  })

  it('rejects an unsupported file inline without uploading it', async () => {
    let uploads = 0
    server.use(
      ...handlers(['evaluations:create']),
      uploadHandler(() => uploads++),
    )
    renderCreate()

    await screen.findByRole('button', { name: 'Upload image' })
    // fireEvent, not user.upload: the latter drops a file the `accept` attribute filters out.
    fireEvent.change(fileInput(), {
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(/JPEG, PNG, or WebP/)
    expect(uploads).toBe(0)
    expect(screen.queryByRole('img', { name: 'Cover image' })).toBeNull()
  })

  it('keeps the form unsubmittable while the upload is in flight', async () => {
    server.use(...handlers(['evaluations:create']), uploadHandler())
    const user = userEvent.setup()
    renderCreate()

    await user.type(await screen.findByLabelText('Title'), 'Busy')
    // Asserted synchronously after the pick: holding the response open trips undici on jsdom's File body.
    fireEvent.change(fileInput(), {
      target: { files: [new File(['x'], 'cover.png', { type: 'image/png' })] },
    })

    expect(screen.getByRole('button', { name: 'Uploading…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create' })).toBeEnabled())
  })

  it('shows an upload failure inline and keeps the current cover', async () => {
    server.use(
      editWithCover('2026/01/01/old.png'),
      ...handlers(['evaluations:update']),
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Request', status: 400, detail: 'Not an image.' },
          { status: 400, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    renderEdit()

    await screen.findByRole('button', { name: 'Replace image' })
    await user.upload(fileInput(), new File(['x'], 'cover.png', { type: 'image/png' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Cover image' })).toHaveAttribute(
      'src',
      'http://localhost/api/v1/images/2026/01/01/old.png',
    )
  })

  it('replaces an existing cover with the new key', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      editWithCover('2026/01/01/old.png'),
      ...handlers(['evaluations:update']),
      uploadHandler(),
      captureUpdate((b) => (body = b)),
    )
    const user = userEvent.setup()
    renderEdit()

    await screen.findByRole('button', { name: 'Replace image' })
    await user.upload(fileInput(), new File(['x'], 'cover.png', { type: 'image/png' }))
    await waitFor(() =>
      expect(screen.getByRole('img', { name: 'Cover image' })).toHaveAttribute(
        'src',
        `http://localhost/api/v1/images/${COVER_KEY}`,
      ),
    )
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.cover_image).toBe(COVER_KEY)
  })

  it('removes the cover — saved as null', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      editWithCover('2026/01/01/old.png'),
      ...handlers(['evaluations:update']),
      captureUpdate((b) => (body = b)),
    )
    const user = userEvent.setup()
    renderEdit()

    await user.click(await screen.findByRole('button', { name: 'Remove image' }))
    expect(screen.queryByRole('img', { name: 'Cover image' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Upload image' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.cover_image).toBeNull()
  })

  it("keeps Replace and Remove when the stored cover can't be displayed", async () => {
    server.use(editWithCover('typed by hand'), ...handlers(['evaluations:update']))
    renderEdit()

    fireEvent.error(await screen.findByRole('img', { name: 'Cover image' }))

    expect(await screen.findByText(/can’t be displayed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Replace image' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Remove image' })).toBeEnabled()
  })
})
