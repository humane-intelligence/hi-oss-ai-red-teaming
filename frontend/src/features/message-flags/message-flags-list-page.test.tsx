import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MessageFlagsListPage } from './message-flags-list-page'
import type { MessageFlagResponse } from '@/lib/api/types'

function flagsHandler(onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/message-flags', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['flags:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/message-flags']}>
          <Routes>
            <Route path="/message-flags" element={<MessageFlagsListPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('MessageFlagsListPage — search', () => {
  it('initial load — no search param in request', async () => {
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('search')).toBeNull()
  })

  it('submitting the search form sends search= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(screen.getByPlaceholderText(/search reason/i)).toBeInTheDocument())
    await user.type(screen.getByPlaceholderText(/search reason/i), 'jailbreak')
    await user.click(screen.getByRole('button', { name: /search/i }))

    await waitFor(() => {
      const withSearch = captured.find((u) => u.searchParams.get('search') === 'jailbreak')
      expect(withSearch).toBeDefined()
    })
  })

  it('empty search omits search param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(screen.getByPlaceholderText(/search reason/i)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /search/i }))

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('search')).toBeNull()
    })
  })
})

describe('MessageFlagsListPage — red_flagged filter', () => {
  it('initial load — no red_flagged param in request', async () => {
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('red_flagged')).toBeNull()
  })

  it('selecting "Red-flagged" sends red_flagged=true', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /red-flagged/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /red-flagged/i, /^Red-flagged$/)

    await waitFor(() => {
      const withFlag = captured.find((u) => u.searchParams.get('red_flagged') === 'true')
      expect(withFlag).toBeDefined()
    })
  })

  it('selecting "Not red-flagged" sends red_flagged=false', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /red-flagged/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /red-flagged/i, /^Not red-flagged$/)

    await waitFor(() => {
      const withFlag = captured.find((u) => u.searchParams.get('red_flagged') === 'false')
      expect(withFlag).toBeDefined()
    })
  })

  it('selecting "All" omits red_flagged param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /red-flagged/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /red-flagged/i, /^Red-flagged$/)
    await chooseOption(user, /red-flagged/i, /^All flags$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('red_flagged')).toBeNull()
    })
  })
})

describe('MessageFlagsListPage — status filter', () => {
  it('initial load — no status param in request', async () => {
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() => expect(captured.length).toBeGreaterThan(0))
    expect(captured[0]!.searchParams.get('status')).toBeNull()
  })

  it('selecting a status sends status= in the query string', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^pending$/)

    await waitFor(() => {
      const withStatus = captured.find((u) => u.searchParams.get('status') === 'pending')
      expect(withStatus).toBeDefined()
    })
  })

  it('selecting "All statuses" omits status param', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /status/i, /^rejected$/)
    await chooseOption(user, /status/i, /^All statuses$/)

    await waitFor(() => {
      const last = captured[captured.length - 1]!
      expect(last.searchParams.get('status')).toBeNull()
    })
  })
})

describe('MessageFlagsListPage — conversation link', () => {
  const EVAL_ID = 'eval-0001-0000-0000-000000000000'
  const CONV_ID = 'conv-0001-0000-0000-000000000000'

  const flag: MessageFlagResponse = {
    id: 'flag-0001-0000-0000-000000000000',
    conversation_id: CONV_ID,
    evaluation_id: EVAL_ID,
    evaluation_group_id: 'grp-0001-0000-0000-000000000000',
    messages: [],
    reason: 'Jailbreak attempt',
    red_flagged: true,
    status: 'pending',
    created_by_id: 'user-0001',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }

  it('renders a link to /evaluations/{eid}/conversations/{cid} for each row', async () => {
    server.use(
      http.get('http://localhost/api/v1/message-flags', () =>
        HttpResponse.json({ items: [flag], total: 1, limit: 20, offset: 0 }),
      ),
    )

    renderPage()

    await waitFor(() => expect(screen.getByRole('link', { name: /open/i })).toBeInTheDocument())
    const link = screen.getByRole('link', { name: /open/i })
    expect(link).toHaveAttribute('href', `/evaluations/${EVAL_ID}/conversations/${CONV_ID}`)
  })
})

describe('MessageFlagsListPage — deleted view', () => {
  const flag: MessageFlagResponse = {
    id: 'flag-0002-0000-0000-000000000000',
    conversation_id: 'conv-0002-0000-0000-000000000000',
    evaluation_id: 'eval-0002-0000-0000-000000000000',
    evaluation_group_id: 'grp-0002-0000-0000-000000000000',
    messages: [],
    reason: 'Deleted flag',
    red_flagged: false,
    status: 'pending',
    created_by_id: 'user-0001',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    deleted_at: '2026-01-02T00:00:00Z',
    deleted_by_id: 'user-0001',
  }

  function renderWithDelete() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(['flags:read', 'flags:delete'])
    render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/message-flags']}>
            <Routes>
              <Route path="/message-flags" element={<MessageFlagsListPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </Wrapper>,
    )
  }

  it('hides the toggle from a caller without flags:delete', async () => {
    server.use(flagsHandler())

    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /status/i })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('combobox', { name: /which flags to show/i })).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" sends deleted=true', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(flagsHandler((url) => captured.push(url)))

    renderWithDelete()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which flags to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which flags to show/i, /recently deleted/i)

    await waitFor(() => {
      expect(captured.some((u) => u.searchParams.get('deleted') === 'true')).toBe(true)
    })
    // Newest tombstone first, as the `deleted` param's own hint recommends.
    const deletedRequest = captured.findLast((u) => u.searchParams.get('deleted') === 'true')
    expect(deletedRequest?.searchParams.get('order_by')).toBe('-deleted_at')
  })

  it('restores a row through the Restore action', async () => {
    const user = userEvent.setup()
    let restored: string | null = null
    server.use(
      http.get('http://localhost/api/v1/message-flags', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        return HttpResponse.json({
          items: deleted && !restored ? [flag] : [],
          total: deleted && !restored ? 1 : 0,
          limit: 20,
          offset: 0,
        })
      }),
      http.post('http://localhost/api/v1/message-flags/:flagId/restore', ({ params }) => {
        restored = params.flagId as string
        return HttpResponse.json({ ...flag, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderWithDelete()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which flags to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which flags to show/i, /recently deleted/i)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /restore/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /restore/i }))

    await waitFor(() => expect(restored).toBe(flag.id))
    // Not just the request: the row has to leave the deleted list, which only happens
    // if the mutation invalidates the listing.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /restore/i })).not.toBeInTheDocument(),
    )
  })
})
