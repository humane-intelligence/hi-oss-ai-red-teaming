import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ReviewsLayout } from './reviews-layout'
import { ReviewQueuePage } from './review-queue-page'
import { ReviewsListPage } from './reviews-list-page'

const FLAG_ID = 'flag-page-0000-0000-000000000000'
const EVAL_ID = 'eval-page-0000-0000-000000000000'
const CONV_ID = 'conv-page-0000-0000-000000000000'
const MSG_ID = 'msg-page-0000-0000-000000000000'

function queueHandlers() {
  return [
    http.get('http://localhost/api/v1/review-queue', ({ request }) => {
      // The layout asks twice: once for the queue total, once for the unassigned tile.
      if (new URL(request.url).searchParams.get('unassigned') === 'true') {
        return HttpResponse.json({ items: [], total: 4, limit: 1, offset: 0 })
      }
      return HttpResponse.json({
        items: [
          {
            submission: {
              id: FLAG_ID,
              evaluation_id: EVAL_ID,
              conversation_id: CONV_ID,
              reason: 'Queue item reason',
              red_flagged: true,
              evaluation_group_id: 'grp-0001-0000-0000-000000000000',
              status: 'pending',
              messages: [
                {
                  id: MSG_ID,
                  role: 'user',
                  status: 'complete',
                  content: 'Queue message content',
                  created_at: '2026-01-01T00:00:00Z',
                },
              ],
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-01T00:00:00Z',
              created_by_id: 'user-0001',
            },
            required_reviews: 1,
            completed_reviews: 0,
            reviews: [],
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      })
    }),
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

function reviewsHandlers() {
  return [
    http.get('http://localhost/api/v1/reviews', () =>
      HttpResponse.json({
        items: [
          {
            id: 'rev-page-0000-0000-000000000000',
            reviewer_id: 'user-0002',
            assigned_by_id: 'user-0001',
            evaluation_id: EVAL_ID,
            message_flag_id: FLAG_ID,
            status: 'approved',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      }),
    ),
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

function allHandlers() {
  return [...queueHandlers(), ...reviewsHandlers()]
}

function renderPage(
  initialPath = '/reviews',
  perms: string[] = ['reviews:update', 'reviews:read'],
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/reviews" element={<ReviewsLayout />}>
              <Route index element={<Navigate to="/reviews/queue" replace />} />
              <Route path="queue" element={<ReviewQueuePage />} />
              <Route path="all" element={<ReviewsListPage />} />
            </Route>
            <Route
              path="/reviews/submissions/:submissionId"
              element={<div>SUBMISSION DETAIL</div>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ReviewsLayout', () => {
  it('renders the Reviews page header', async () => {
    server.use(...allHandlers())
    renderPage()
    expect(screen.getByRole('heading', { name: /reviews/i })).toBeInTheDocument()
  })

  it('redirects /reviews to the Queue tab (active by default)', async () => {
    server.use(...queueHandlers())
    renderPage()
    const queueTab = screen.getByRole('tab', { name: /queue/i })
    expect(queueTab).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /all reviews/i })).toHaveAttribute(
      'aria-selected',
      'false',
    )
  })

  it('default route shows queue content', async () => {
    server.use(...queueHandlers())
    renderPage()
    await waitFor(() => expect(screen.getByText('Queue item reason')).toBeInTheDocument())
    // The queue item has no scenario_id, so its review rigor is the no-challenge default.
    expect(screen.getByText(/no challenge/i)).toBeInTheDocument()
  })

  it('clicking the All reviews tab shows the reviews list', async () => {
    const user = userEvent.setup()
    server.use(...allHandlers())
    renderPage()

    await user.click(screen.getByRole('tab', { name: /all reviews/i }))

    await waitFor(() =>
      expect(screen.getByRole('columnheader', { name: /reviewer/i })).toBeInTheDocument(),
    )
  })

  it('/reviews/all activates the All reviews tab', async () => {
    server.use(...reviewsHandlers())
    renderPage('/reviews/all')
    expect(screen.getByRole('tab', { name: /all reviews/i })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByRole('tab', { name: /queue/i })).toHaveAttribute('aria-selected', 'false')
    await waitFor(() =>
      expect(screen.getByRole('columnheader', { name: /reviewer/i })).toBeInTheDocument(),
    )
  })

  it('shows the My reviews tab for a reviewer (reviews:update)', () => {
    server.use(...allHandlers())
    renderPage()
    expect(screen.getByRole('tab', { name: /my reviews/i })).toBeInTheDocument()
  })

  it('read-only viewer: no My reviews tab, and both stat tiles say (yours)', () => {
    server.use(...allHandlers())
    renderPage('/reviews', ['reviews:read'])
    expect(screen.queryByRole('tab', { name: /my reviews/i })).toBeNull()
    expect(screen.getByRole('tab', { name: /queue/i })).toBeInTheDocument()
    expect(screen.getByText(/awaiting review \(yours\)/i)).toBeInTheDocument()
    expect(screen.getByText(/^unassigned \(yours\)$/i)).toBeInTheDocument()
    expect(screen.queryByText(/assigned to me/i)).toBeNull()
    expect(screen.queryByText(/reviewed by me/i)).toBeNull()
  })

  it('read-only viewer: All reviews row opens the submission detail', async () => {
    const user = userEvent.setup()
    server.use(...allHandlers())
    renderPage('/reviews/all', ['reviews:read'])
    // Wait for the loaded state (header + 1 data row); the skeleton renders more rows
    // and isn't clickable. Target the row element — the status text collides with the filter <option>.
    await waitFor(() => expect(screen.getAllByRole('row')).toHaveLength(2))
    const rows = screen.getAllByRole('row')
    await user.click(rows[rows.length - 1]!)
    await waitFor(() => expect(screen.getByText('SUBMISSION DETAIL')).toBeInTheDocument())
  })
})

describe('ReviewsLayout — unassigned shortcut', () => {
  it('shows how many flags nobody is reviewing, as a link into that filtered queue', async () => {
    server.use(...allHandlers())
    renderPage()

    const tile = await screen.findByRole('link', { name: /unassigned/i })
    expect(tile).toHaveAttribute('href', '/reviews/queue?unassigned=1')
    await waitFor(() => expect(tile).toHaveTextContent('4'))
  })

  it('says whose flags it counts when the queue is scoped to the reader’s own', async () => {
    server.use(...allHandlers())
    // No `reviews:update` — the backend narrows this reader's queue to flags they raised, and the
    // sibling tile already says "(yours)" for exactly this reason.
    renderPage('/reviews', ['reviews:read'])

    expect(await screen.findByRole('link', { name: /unassigned \(yours\)/i })).toBeInTheDocument()
  })
})

describe('ReviewsLayout — the unassigned tile before its count lands', () => {
  it('shows an em dash rather than a clickable zero while the count is in flight', async () => {
    server.use(
      ...reviewsHandlers(),
      http.get('http://localhost/api/v1/review-queue', ({ request }) => {
        const url = new URL(request.url)
        if (url.searchParams.get('unassigned') === 'true') return new Promise<never>(() => {})
        return HttpResponse.json({ items: [], total: 40, limit: 1, offset: 0 })
      }),
      http.get('http://localhost/api/v1/auth/users', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderPage('/reviews/queue', ['reviews:read', 'reviews:update'])

    const tile = await screen.findByRole('link', { name: /unassigned/i })
    expect(tile).toHaveTextContent('—')
    expect(tile).not.toHaveTextContent('0')
  })
})

describe('ReviewsLayout — the tile and the queue page number', () => {
  it('lands on the first page of the filtered queue even from page two', async () => {
    const user = userEvent.setup()
    const seen: URL[] = []
    server.use(
      ...reviewsHandlers(),
      http.get('http://localhost/api/v1/review-queue', ({ request }) => {
        const url = new URL(request.url)
        seen.push(url)
        const unassigned = url.searchParams.get('unassigned') === 'true'
        const offset = Number(url.searchParams.get('offset') ?? '0')
        return HttpResponse.json({
          items: unassigned || offset > 0 ? [] : [],
          total: unassigned ? 4 : 40,
          limit: Number(url.searchParams.get('limit') ?? '20'),
          offset,
        })
      }),
      http.get('http://localhost/api/v1/auth/users', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    // Page two of the unfiltered queue, then the tile — a search-param-only navigation into a
    // route that does not remount, which is what makes a page kept outside the URL go stale.
    renderPage('/reviews/queue?offset=20', ['reviews:read', 'reviews:update'])
    await waitFor(() => expect(seen.some((u) => u.searchParams.get('offset') === '20')).toBe(true))

    await user.click(await screen.findByRole('link', { name: /unassigned/i }))

    // Only the queue's own request answers this — the layout's tile query carries
    // `unassigned=true` and `offset=0` on its own (`limit=1`), so asserting on the last request of
    // any kind would pass with the page left on 20.
    await waitFor(() => {
      const queueRequests = seen.filter((u) => u.searchParams.get('limit') !== '1')
      const last = queueRequests[queueRequests.length - 1]!
      expect(last.searchParams.get('unassigned')).toBe('true')
      expect(last.searchParams.get('offset')).toBe('0')
    })
  })
})
