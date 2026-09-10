import { describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { LicenseDetailPage } from './license-detail-page'
import type { DataLicenseResponse } from '@/lib/api/types'

const ID = '00000000-0000-0000-0000-0000000000d1'

function license(overrides: Partial<DataLicenseResponse> = {}): DataLicenseResponse {
  return {
    id: ID,
    spdx_id: null,
    name: 'Acme Internal License',
    version: '1.0',
    short_description: 'Internal-only dataset use.',
    reference_url: 'https://acme.example/license',
    is_curated: false,
    has_content: true,
    is_no_license: false,
    text_managed_in_code: false,
    protects_conversation_data: false,
    is_default: false,
    created_by_id: '1',
    content: 'ACME INTERNAL DATA LICENSE v1.0',
    ...overrides,
  }
}

function renderDetail(row: DataLicenseResponse, perms = ['licenses:update', 'licenses:delete']) {
  server.use(http.get(`http://localhost/api/v1/licenses/${ID}`, () => HttpResponse.json(row)))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/licenses/${ID}`]}>
          <Routes>
            <Route path="/licenses/:id" element={<LicenseDetailPage />} />
            <Route path="/licenses" element={<div>list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('LicenseDetailPage', () => {
  it('shows Edit and Delete for a user-authored license the caller owns', async () => {
    renderDetail(license())
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
    expect(screen.getByText('ACME INTERNAL DATA LICENSE v1.0')).toBeInTheDocument()
  })

  it('badges a licence that protects conversation data', async () => {
    renderDetail(license({ protects_conversation_data: true }))
    await waitFor(() => expect(screen.getByText('protected')).toBeInTheDocument())
  })

  it('says so plainly when a licence does not protect conversation data', async () => {
    // The negative half matters: a field hard-coded to either answer would satisfy the positive test
    // alone, and the badge is the only place this flag reaches a reader of the detail page.
    renderDetail(license())
    await waitFor(() => expect(screen.getByText('Not protected')).toBeInTheDocument())
    expect(screen.queryByText('protected')).not.toBeInTheDocument()
  })

  it('marks a tombstoned licence and offers none of its actions', async () => {
    // The read serves tombstones so their text stays legible for the groups still referencing them,
    // but every write path resolves live rows only — an offered Edit or Delete would 404.
    renderDetail(license({ deleted_at: '2026-08-20T10:00:00Z' }))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    expect(screen.getByText('deleted')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
    expect(screen.getByText('ACME INTERNAL DATA LICENSE v1.0')).toBeInTheDocument()
  })

  it('withholds the curated text edit on a tombstoned licence', async () => {
    // Same live-only write rule, reached through the other gate — the curated text path 404s too.
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC0-1.0',
        name: 'CC0 1.0',
        content: '',
        has_content: false,
        text_managed_in_code: false,
        deleted_at: '2026-08-20T10:00:00Z',
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'CC0 1.0' })).toBeInTheDocument(),
    )

    expect(screen.queryByRole('button', { name: /add license text/i })).not.toBeInTheDocument()
  })

  it('offers no text editor for a licence whose text the catalog ships', async () => {
    // The API refuses that edit — the resync would revert it — so the console must not offer a
    // control that can only fail. It gates on the server's own predicate (`text_managed_in_code`),
    // not on the row being the "no license" entry: that proxy holds only while the sentinel is the
    // single catalog entry carrying a text. The sibling case (a curated row the catalog leaves
    // empty) keeps its editor, which is the test below.
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: null,
        name: 'No license',
        text_managed_in_code: true,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'No license' })).toBeInTheDocument(),
    )

    expect(screen.queryByRole('button', { name: /license text/i })).toBeNull()
  })

  it('offers the text editor for a curated license the catalog ships no text for', async () => {
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC0-1.0',
        name: 'CC0 1.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'CC0 1.0' })).toBeInTheDocument(),
    )

    expect(screen.getByRole('button', { name: /add license text/i })).toBeInTheDocument()
  })

  it('refuses to save an empty license text', async () => {
    // `content` is `min_length=1` server-side, so an empty save would surface a raw 422 on a click
    // the UI should never have allowed.
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC0-1.0',
        name: 'CC0 1.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'CC0 1.0' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /add license text/i }))

    const textarea = await screen.findByLabelText('License text')
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled()
    fireEvent.change(textarea, { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled()
    fireEvent.change(textarea, { target: { value: 'CC0 legal text' } })
    expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled()
  })

  it('hides Edit and Delete on a curated license even with the permissions', async () => {
    renderDetail(
      license({ is_curated: true, created_by_id: null, spdx_id: 'CC-BY-4.0', name: 'CC BY 4.0' }),
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'CC BY 4.0' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
  })

  it("hides Edit and Delete on another user's license without licenses:manage", async () => {
    renderDetail(license({ created_by_id: 'someone-else' }))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
  })

  it("shows Edit and Delete on another user's license with licenses:manage", async () => {
    renderDetail(license({ created_by_id: 'someone-else' }), [
      'licenses:update',
      'licenses:delete',
      'licenses:manage',
    ])
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
  })

  it('offers the curated text edit to a licenses:manage holder and sends content alone', async () => {
    // The catalog ships no text for most curated entries and the resync leaves the column alone,
    // so this is the one field the API accepts on a curated row.
    let body: Record<string, unknown> | undefined
    server.use(
      http.patch(`http://localhost/api/v1/licenses/${ID}`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          license({ is_curated: true, created_by_id: null, content: 'CC TEXT' }),
        )
      }),
    )
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC-BY-4.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: /license text/i }))
    fireEvent.change(await screen.findByLabelText(/license text/i), {
      target: { value: 'CC TEXT' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).toBeDefined())
    expect(body).toEqual({ content: 'CC TEXT' })
  })

  it('keeps the text dialog open when the save fails', async () => {
    // A failed PATCH must not be read as saved: the global mutation-error toast reports it and the
    // draft stays on screen, so the operator can retry without retyping the text.
    let attempted = false
    server.use(
      http.patch(`http://localhost/api/v1/licenses/${ID}`, () => {
        attempted = true
        return HttpResponse.json({ detail: 'nope' }, { status: 403 })
      }),
    )
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC-BY-4.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: /license text/i }))
    fireEvent.change(await screen.findByLabelText(/license text/i), { target: { value: 'TEXT' } })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    // Wait for the request to land and Save to come back from `Saving…` first. Asserting the
    // textarea straight after the click holds before the PATCH ever rejects, so a dialog that
    // closed on the failure — losing the typed text — would read as green.
    await waitFor(() => expect(attempted).toBe(true))
    await waitFor(() => expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled())
    expect(screen.getByLabelText(/license text/i)).toHaveValue('TEXT')
  })

  it('holds Cancel while the save is in flight', async () => {
    // `busy` already keeps Esc and the backdrop from closing the dialog mid-write; an enabled Cancel
    // is the third way out, and it closes the dialog while the PATCH still lands — so the operator
    // sees the write aborted and the licence takes the text anyway.
    server.use(
      http.patch(`http://localhost/api/v1/licenses/${ID}`, async () => {
        await delay('infinite')
        return HttpResponse.json(license())
      }),
    )
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC-BY-4.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: /license text/i }))
    fireEvent.change(await screen.findByLabelText(/license text/i), { target: { value: 'TEXT' } })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: /^cancel$/i })).toBeDisabled())
  })

  it('reopening the editor shows the stored text, not an abandoned draft', async () => {
    // The extraction moved this reset out of the click handler and into the dialog's `onOpen`;
    // without it, a cancelled edit comes back the next time the operator opens the editor.
    renderDetail(
      license({ is_curated: true, created_by_id: null, spdx_id: 'CC-BY-4.0', content: 'CC TEXT' }),
      ['licenses:update', 'licenses:manage'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: /license text/i }))
    fireEvent.change(await screen.findByLabelText(/license text/i), {
      target: { value: 'ABANDONED' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    fireEvent.click(screen.getByRole('button', { name: /license text/i }))

    expect(await screen.findByLabelText(/license text/i)).toHaveValue('CC TEXT')
  })

  it('withholds the curated text edit without licenses:manage', async () => {
    renderDetail(
      license({
        is_curated: true,
        created_by_id: null,
        spdx_id: 'CC-BY-4.0',
        content: '',
        has_content: false,
        is_no_license: false,
        text_managed_in_code: false,
      }),
      ['licenses:update'],
    )
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Acme Internal License' })).toBeInTheDocument(),
    )

    expect(screen.queryByRole('button', { name: /license text/i })).toBeNull()
  })
})
