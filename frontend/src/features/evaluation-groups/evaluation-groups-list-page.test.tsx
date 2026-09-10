import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { EvaluationGroupsListPage } from './evaluation-groups-list-page'

function groupsHandler(onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })
  })
}

function orgsHandler(items: { id: string; name: string }[] = []) {
  return http.get('http://localhost/api/v1/organizations', () =>
    HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 }),
  )
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluation_groups:read', 'organizations:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/evaluation-groups']}>
          <Routes>
            <Route path="/evaluation-groups" element={<EvaluationGroupsListPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationGroupsListPage — access_level filter', () => {
  it('initial load — no access_level param in request', async () => {
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('access_level')).toBeNull()
  })

  it('selecting "Public" sends access_level=public', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /access/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /access/i, /^Public$/)

    await waitFor(() => {
      const withAccess = captured.find((u) => u.searchParams.get('access_level') === 'public')
      expect(withAccess).toBeDefined()
    })
  })

  it('selecting "All" omits access_level param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /access/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /access/i, /^Invitation only$/)
    await chooseOption(user, /access/i, /^All access levels$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('access_level')).toBeNull()
    })
  })
})

describe('EvaluationGroupsListPage — organization column', () => {
  it('resolves the owning organization id to its name', async () => {
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups', () =>
        HttpResponse.json({
          items: [
            {
              id: 'g1',
              title: 'Org group',
              description: 'd',
              created_by_id: 'u1',
              status: 'draft',
              access_level: 'organization',
              organization_id: 'org-1',
              start_date: '2026-01-01',
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-01T00:00:00Z',
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        }),
      ),
      orgsHandler([{ id: 'org-1', name: 'Acme Corp' }]),
    )

    renderPage()

    expect(await screen.findByText('Acme Corp')).toBeInTheDocument()
  })
})

describe('EvaluationGroupsListPage — status filter', () => {
  it('initial load — no status param in request', async () => {
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('status')).toBeNull()
  })

  it('selecting a status sends status= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^published$/)

    await waitFor(() => {
      const withStatus = captured.find((u) => u.searchParams.get('status') === 'published')
      expect(withStatus).toBeDefined()
    })
  })

  it('selecting "All statuses" omits status param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      groupsHandler((url) => captured.push(url)),
      orgsHandler(),
    )

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^draft$/)
    await chooseOption(user, /status/i, /^All statuses$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('status')).toBeNull()
    })
  })
})
