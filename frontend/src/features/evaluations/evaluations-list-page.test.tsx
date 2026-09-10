import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { EvaluationsListPage } from './evaluations-list-page'

function evaluationsHandler(onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/evaluations', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })
  })
}

function renderPage(permissions: string[] = ['evaluations:read']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/evaluations']}>
          <Routes>
            <Route path="/evaluations" element={<EvaluationsListPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
  // Returned so a test can invalidate the way a group mutation does.
  return qc
}

describe('EvaluationsListPage — sort', () => {
  it('initial load sends order_by=-created_at', async () => {
    const captured: URL[] = []
    server.use(evaluationsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('order_by')).toBe('-created_at')
  })

  it('clicking a sortable header sends order_by in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(evaluationsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(screen.getByRole('button', { name: /title/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /title/i }))

    await waitFor(() => {
      const withSort = captured.find((u) => u.searchParams.get('order_by') === 'title')
      expect(withSort).toBeDefined()
    })
  })
})

describe('EvaluationsListPage — status filter', () => {
  it('initial load — no status param in request', async () => {
    const captured: URL[] = []
    server.use(evaluationsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('status')).toBeNull()
  })

  it('selecting a status sends status= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(evaluationsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^draft$/)

    await waitFor(() => {
      const withStatus = captured.find((u) => u.searchParams.get('status') === 'draft')
      expect(withStatus).toBeDefined()
    })
  })

  it('selecting "All statuses" omits status param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(evaluationsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    // first pick a status, then reset
    await chooseOption(user, /status/i, /^approved$/)
    await chooseOption(user, /status/i, /^All statuses$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('status')).toBeNull()
    })
  })
})

const GROUP_ON_PAGE_TWO = '3f2a1b0c-1111-2222-3333-444455556666'

// The endpoint caps `limit` at 100, so a lookup that reads one page cannot name group 101.
function groupsHandler(onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
    const url = new URL(request.url)
    onRequest?.(url)
    const offset = Number(url.searchParams.get('offset') ?? '0')
    const items =
      offset === 0
        ? Array.from({ length: 100 }, (_, i) => ({ id: `grp-${i}`, title: `Group ${i}` }))
        : [{ id: GROUP_ON_PAGE_TWO, title: 'Hundred-and-first group' }]
    return HttpResponse.json({ items, total: 101, limit: 100, offset })
  })
}

function oneEvaluationIn(groupId: string) {
  return http.get('http://localhost/api/v1/evaluations', () =>
    HttpResponse.json({
      items: [
        {
          id: 'ev-1',
          title: 'Prompt injection',
          description: null,
          evaluation_group_id: groupId,
          created_by_id: 'u1',
          status: 'new',
          cover_image: null,
          mask_models_enabled: false,
          tags_enabled: false,
          tags_restricted: false,
          data_license_id: null,
          effective_license: null,
          rejection_reason: null,
          models: [],
          created_at: '2026-08-11T00:00:00Z',
          updated_at: '2026-08-11T00:00:00Z',
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    }),
  )
}

describe('EvaluationsListPage — the Group column', () => {
  it('names a group that only appears on the second lookup page', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      groupsHandler((url) => seen.push(url)),
    )

    renderPage(['evaluations:read', 'evaluation_groups:read'])

    expect(await screen.findByText('Hundred-and-first group')).toBeInTheDocument()
    expect(seen.map((u) => u.searchParams.get('offset'))).toEqual(['0', '100'])
  })

  it('refetches the lookup when a group mutation invalidates, so a fresh group gets named', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      groupsHandler((url) => seen.push(url)),
    )

    const qc = renderPage(['evaluations:read', 'evaluation_groups:read'])
    expect(await screen.findByText('Hundred-and-first group')).toBeInTheDocument()
    const walked = seen.length

    // The exact key `useInvalidateGroup` fires (features/evaluation-groups/mutations.ts) after a
    // group is created, renamed or deleted. The lookup's `staleTime` outlives the global default, so
    // a key outside this prefix is never refetched and the cell keeps naming the world as it was.
    qc.invalidateQueries({ queryKey: ['evaluation-groups'] })

    await waitFor(() => expect(seen.length).toBeGreaterThan(walked))
  })

  it('leads nowhere rather than linking a name it could not resolve', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      groupsHandler((url) => seen.push(url)),
    )

    renderPage(['evaluations:read'])

    // This persona lacks `evaluation_groups:read`, which is also what guards the group page, so the
    // cell must not ship a link into a permission wall — and must not print the raw id either.
    expect(await screen.findByText('—')).toBeInTheDocument()
    expect(document.querySelector(`a[href="/evaluation-groups/${GROUP_ON_PAGE_TWO}"]`)).toBeNull()
    expect(screen.queryByText(/3f2a1b0c/)).toBeNull()
    // What separates "the gate is closed" from "the request failed": the query never ran.
    expect(seen).toHaveLength(0)
  })

  it('shows the names are still loading rather than naming the group wrong', async () => {
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      // Never settles: the lookup stays in flight for the whole test.
      http.get('http://localhost/api/v1/evaluation-groups', () => new Promise<never>(() => {})),
    )

    renderPage(['evaluations:read', 'evaluation_groups:read'])

    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    // In flight is not the same answer as looked-up-and-missing: neither a link, nor the em dash
    // that says the lookup came back without a name.
    expect(document.querySelector(`a[href="/evaluation-groups/${GROUP_ON_PAGE_TWO}"]`)).toBeNull()
    expect(screen.queryByText('—')).toBeNull()
  })

  it('stops walking rather than chasing a total it can never reach', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      // A total the pages never satisfy — without a cap the client would page forever.
      http.get('http://localhost/api/v1/evaluation-groups', ({ request }) => {
        seen.push(new URL(request.url))
        return HttpResponse.json({
          items: Array.from({ length: 100 }, (_, i) => ({
            id: `g-${seen.length}-${i}`,
            title: 'x',
          })),
          total: 999_999,
          limit: 100,
          offset: 0,
        })
      }),
    )

    renderPage(['evaluations:read', 'evaluation_groups:read'])

    // Twenty sequential round trips have to finish inside the async window, so the default 1000 ms
    // is too tight on a loaded runner.
    await screen.findByText('—', undefined, { timeout: 5000 })
    expect(seen).toHaveLength(20)
  })

  it('widens the lookup to every group for a manager, whose evaluation list is already that wide', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      groupsHandler((url) => seen.push(url)),
    )

    renderPage(['evaluations:read', 'evaluation_groups:read', 'evaluation_groups:manage'])

    expect(await screen.findByText('Hundred-and-first group')).toBeInTheDocument()
    expect(seen.every((u) => u.searchParams.get('all_groups') === 'true')).toBe(true)
  })

  it('leaves the scope alone for a caller who cannot manage groups', async () => {
    const seen: URL[] = []
    server.use(
      oneEvaluationIn(GROUP_ON_PAGE_TWO),
      groupsHandler((url) => seen.push(url)),
    )

    renderPage(['evaluations:read', 'evaluation_groups:read'])

    expect(await screen.findByText('Hundred-and-first group')).toBeInTheDocument()
    // Sending it without the permission is a 403, so it must not be sent.
    expect(seen.every((u) => u.searchParams.get('all_groups') === null)).toBe(true)
  })
})
