import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { LicensesListPage } from './licenses-list-page'
import type { DataLicenseSummary } from '@/lib/api/types'

const curated: DataLicenseSummary = {
  id: '00000000-0000-0000-0000-0000000000c1',
  spdx_id: 'CC-BY-4.0',
  name: 'Creative Commons Attribution 4.0',
  version: '4.0',
  short_description: 'Share and adapt with attribution.',
  reference_url: 'https://creativecommons.org/licenses/by/4.0/',
  is_curated: true,
  is_default: true,
  has_content: false,
  is_no_license: false,
  text_managed_in_code: false,
  protects_conversation_data: false,
  created_by_id: null,
}

const custom: DataLicenseSummary = {
  id: '00000000-0000-0000-0000-0000000000a1',
  spdx_id: null,
  name: 'Acme Internal License',
  version: '1.0',
  short_description: 'Internal-only dataset use.',
  reference_url: null,
  is_curated: false,
  is_default: false,
  has_content: true,
  is_no_license: false,
  text_managed_in_code: false,
  protects_conversation_data: false,
  created_by_id: '1',
}

function renderPage(
  rows: DataLicenseSummary[] = [curated, custom],
  perms: string[] = ['licenses:create'],
) {
  server.use(
    http.get('http://localhost/api/v1/licenses', () =>
      HttpResponse.json({ items: rows, total: rows.length, limit: 100, offset: 0 }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <LicensesListPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('LicensesListPage', () => {
  it('renders curated and custom rows with type and default badges', async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Creative Commons Attribution 4.0')).toBeInTheDocument(),
    )
    expect(screen.getByText('Acme Internal License')).toBeInTheDocument()
    expect(screen.getByText('Curated')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument()
    expect(screen.getByText('Default')).toBeInTheDocument()
    // user-authored has no SPDX id → em dash
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('marks a licence that protects conversation data', async () => {
    // The Type column is where an operator scans the catalog, and the flag decides whether
    // transcripts written under the licence are encrypted — it was readable only on the detail page.
    renderPage([{ ...custom, protects_conversation_data: true }])

    await waitFor(() => expect(screen.getByText('Acme Internal License')).toBeInTheDocument())
    expect(screen.getByText('protected')).toBeInTheDocument()
  })

  it('filters client-side by name or SPDX', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('Acme Internal License')).toBeInTheDocument())

    await user.type(screen.getByLabelText('Filter licenses'), 'acme')

    await waitFor(() => expect(screen.queryByText('Creative Commons Attribution 4.0')).toBeNull())
    expect(screen.getByText('Acme Internal License')).toBeInTheDocument()
  })

  it('shows the New license button only with licenses:create', async () => {
    renderPage([curated, custom], [])
    await waitFor(() =>
      expect(screen.getByText('Creative Commons Attribution 4.0')).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /new license/i })).toBeNull()
  })
})

describe('LicensesListPage — recently deleted', () => {
  // A local renderer: `renderPage` installs its own `/licenses` handler, which would
  // shadow the request-recording one these tests need.
  function renderBare(perms: string[]) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(perms)
    render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <LicensesListPage />
          </MemoryRouter>
        </QueryClientProvider>
      </Wrapper>,
    )
  }

  const tombstone: DataLicenseSummary = {
    ...custom,
    id: '00000000-0000-0000-0000-0000000000d1',
    name: 'Retired Internal License',
    deleted_at: '2026-01-02T00:00:00Z',
    deleted_by_id: '1',
  }

  it('is hidden from a caller without licenses:delete', async () => {
    renderPage([curated], ['licenses:create'])

    await screen.findByText('Creative Commons Attribution 4.0')
    expect(screen.queryByRole('button', { name: /recently deleted/i })).not.toBeInTheDocument()
  })

  it('fetches nothing until the disclosure is opened', async () => {
    const user = userEvent.setup()
    const calls: string[] = []
    server.use(
      http.get('http://localhost/api/v1/licenses', ({ request }) => {
        const url = new URL(request.url)
        calls.push(url.searchParams.get('deleted') ?? 'live')
        return HttpResponse.json({
          items: url.searchParams.get('deleted') === 'true' ? [tombstone] : [custom],
          total: 1,
          limit: 100,
          offset: 0,
        })
      }),
    )
    renderBare(['licenses:delete'])

    await screen.findByText('Acme Internal License')
    expect(calls).toEqual(['live'])

    await user.click(screen.getByRole('button', { name: /recently deleted/i }))

    await screen.findByText('Retired Internal License')
    expect(calls).toContain('true')
  })

  it('restores a licence and drops it from the list', async () => {
    const user = userEvent.setup()
    let restored = false
    server.use(
      http.get('http://localhost/api/v1/licenses', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        if (!deleted) return HttpResponse.json({ items: [custom], total: 1, limit: 100, offset: 0 })
        const items = restored ? [] : [tombstone]
        return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/licenses/${tombstone.id}/restore`, () => {
        restored = true
        return HttpResponse.json({
          ...tombstone,
          deleted_at: null,
          deleted_by_id: null,
          content: 'X',
        })
      }),
    )
    renderBare(['licenses:delete'])
    await screen.findByText('Acme Internal License')

    await user.click(screen.getByRole('button', { name: /recently deleted/i }))
    await user.click(
      await screen.findByRole('button', { name: /restore license: retired internal license/i }),
    )

    await waitFor(() =>
      expect(screen.queryByText('Retired Internal License')).not.toBeInTheDocument(),
    )
    expect(restored).toBe(true)
  })
})
