import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { LicenseFormPage } from './license-form-page'
import type { DataLicenseResponse } from '@/lib/api/types'

const ID = '00000000-0000-0000-0000-0000000000f1'

function existing(overrides: Partial<DataLicenseResponse> = {}): DataLicenseResponse {
  return {
    id: ID,
    spdx_id: null,
    name: 'Acme Internal License',
    version: '1.0',
    short_description: 'Internal-only.',
    reference_url: null,
    is_curated: false,
    has_content: true,
    is_no_license: false,
    text_managed_in_code: false,
    protects_conversation_data: false,
    is_default: false,
    created_by_id: '1',
    content: 'Existing text.',
    ...overrides,
  }
}

function renderForm(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/licenses/new" element={<LicenseFormPage />} />
          <Route path="/licenses/:id/edit" element={<LicenseFormPage />} />
          <Route path="/licenses/:id" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('LicenseFormPage', () => {
  it('sends the create body with empty optionals as null', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/licenses', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: ID }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderForm('/licenses/new')

    await user.type(screen.getByLabelText('Name'), 'Acme License')
    await user.type(screen.getByLabelText('Short description'), 'Internal use.')
    await user.type(screen.getByLabelText('License text'), 'Full legal text.')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({
      name: 'Acme License',
      version: null,
      short_description: 'Internal use.',
      content: 'Full legal text.',
      reference_url: null,
      protects_conversation_data: false,
    })
  })

  it('sends the protection flag when the operator ticks it', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/licenses', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: ID }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderForm('/licenses/new')

    await user.type(screen.getByLabelText('Name'), 'Acme Confidential 1.0')
    await user.type(screen.getByLabelText('Short description'), 'No redistribution.')
    await user.type(screen.getByLabelText('License text'), 'Full legal text.')
    await user.click(screen.getByRole('checkbox', { name: /protect conversation data/i }))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.protects_conversation_data).toBe(true)
  })

  it('blocks submit when a required field is empty', async () => {
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/licenses', () => {
        posted = true
        return HttpResponse.json({ id: ID }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderForm('/licenses/new')

    // Name + short_description filled, license text left empty → zod blocks, no POST.
    await user.type(screen.getByLabelText('Name'), 'Acme License')
    await user.type(screen.getByLabelText('Short description'), 'Internal use.')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('Required')).toBeInTheDocument()
    expect(posted).toBe(false)
  })

  it('prefills from the existing license and PATCHes on save', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/licenses/${ID}`, () => HttpResponse.json(existing())),
      http.patch(`http://localhost/api/v1/licenses/${ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(existing())
      }),
    )
    const user = userEvent.setup()
    renderForm(`/licenses/${ID}/edit`)

    const name = await screen.findByLabelText('Name')
    await waitFor(() => expect(name).toHaveValue('Acme Internal License'))
    await user.clear(name)
    await user.type(name, 'Acme Renamed')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.name).toBe('Acme Renamed')
    expect(captured!.content).toBe('Existing text.')
  })

  it('makes a curated license read-only and says why', async () => {
    // Dropping the per-field guard left curated rows with no affordance at all: every control
    // enabled, and a Save that 403s silently. The gate lives on the whole row, so the form says so.
    server.use(
      http.get(`http://localhost/api/v1/licenses/${ID}`, () =>
        HttpResponse.json(existing({ is_curated: true, created_by_id: null })),
      ),
    )
    renderForm(`/licenses/${ID}/edit`)

    expect(await screen.findByText(/managed in code/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('Name')).toBeDisabled())
    expect(screen.getByRole('checkbox', { name: /protect conversation data/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    // The way off a read-only page: disabling it with the rest would leave only Back and the crumb.
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('does not point at an editor for a curated license whose text ships in code', async () => {
    // `No license` is that row, and it is the one curated entry carrying the protection flag — so
    // the promise lands on exactly the licence whose page renders no editor.
    server.use(
      http.get(`http://localhost/api/v1/licenses/${ID}`, () =>
        HttpResponse.json(
          existing({ is_curated: true, created_by_id: null, text_managed_in_code: true }),
        ),
      ),
    )
    renderForm(`/licenses/${ID}/edit`)

    expect(await screen.findByText(/ships in the catalog too/i)).toBeInTheDocument()
    expect(screen.queryByText(/carries the editor/i)).not.toBeInTheDocument()
  })

  it('keeps the protection flag through an edit that does not touch it', async () => {
    // The round-trip, not the toggle: a `reset()` that hard-coded `false` would pass every other
    // test here and silently strip protection from a licence on any unrelated edit.
    let captured: Record<string, unknown> | null = null
    const protectedLicense = existing({ protects_conversation_data: true })
    server.use(
      http.get(`http://localhost/api/v1/licenses/${ID}`, () => HttpResponse.json(protectedLicense)),
      http.patch(`http://localhost/api/v1/licenses/${ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(protectedLicense)
      }),
    )
    const user = userEvent.setup()
    renderForm(`/licenses/${ID}/edit`)

    const checkbox = await screen.findByRole('checkbox', { name: /protect conversation data/i })
    await waitFor(() => expect(checkbox).toBeChecked())
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.protects_conversation_data).toBe(true)
  })
})
