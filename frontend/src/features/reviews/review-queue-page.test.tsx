import { beforeEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ReviewQueuePage } from './review-queue-page'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'

function queueItem(flagId: string, reason: string) {
  return {
    submission: {
      id: flagId,
      conversation_id: 'conv-0001-0000-0000-000000000000',
      messages: [
        {
          id: 'm1',
          role: 'assistant',
          status: 'complete',
          content: 'x',
          slot: null,
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      reason,
      red_flagged: true,
      comment: '',
      status: 'pending',
      created_by_id: 'user-0001',
      evaluation_id: EVAL_ID,
      evaluation_group_id: 'grp-0001-0000-0000-000000000000',
      scenario_id: 'scn-0001-0000-0000-000000000000',
      task_id: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    required_reviews: 1,
    completed_reviews: 0,
    reviews: [],
  }
}

// Page 1 (offset 0) and page 2 (offset 20) return different flags; total > page size so Next is enabled.
// The `unassigned` branch answers with its own single flag, and every call records the URL it was
// asked for — the filter is a query parameter, so the request is the only place to see it.
let lastQueueUrl: URL | null = null

beforeEach(() => {
  // Module-level, so a stale value from the previous test would satisfy an assertion before any
  // request of this one lands.
  lastQueueUrl = null
})

function queueHandler() {
  return http.get('http://localhost/api/v1/review-queue', ({ request }) => {
    lastQueueUrl = new URL(request.url)
    const offset = Number(lastQueueUrl.searchParams.get('offset') ?? '0')
    if (lastQueueUrl.searchParams.get('unassigned') === 'true') {
      return HttpResponse.json({
        items: [queueItem('flag-un-0000-0000-000000000000', 'nobody reviewing this')],
        total: 1,
        limit: 20,
        offset,
      })
    }
    const items =
      offset >= 20
        ? [queueItem('flag-p2-0000-0000-000000000000', 'page two flag')]
        : [queueItem('flag-p1-0000-0000-000000000000', 'page one flag')]
    return HttpResponse.json({ items, total: 40, limit: 20, offset })
  })
}

function renderQueue(initialPath = '/reviews/queue') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['reviews:read', 'reviews:create'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/reviews/queue" element={<ReviewQueuePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ReviewQueuePage — batch selection', () => {
  it('shows the batch bar with a count when a flag is checked', async () => {
    const user = userEvent.setup()
    server.use(queueHandler())
    renderQueue()

    await waitFor(() => expect(screen.getByText('page one flag')).toBeInTheDocument())
    // checkboxes: [select-all, row-1]
    await user.click(screen.getAllByRole('checkbox')[1]!)
    expect(screen.getByText(/1 flag selected/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /assign reviewers/i })).toBeInTheDocument()
  })

  it('Clear empties the selection and hides the batch bar', async () => {
    const user = userEvent.setup()
    server.use(queueHandler())
    renderQueue()

    await waitFor(() => expect(screen.getByText('page one flag')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[1]!)
    expect(screen.getByText(/1 flag selected/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^clear$/i }))
    expect(screen.queryByText(/flag selected/i)).toBeNull()
  })

  it('clears the selection when the page changes', async () => {
    const user = userEvent.setup()
    server.use(queueHandler())
    renderQueue()

    await waitFor(() => expect(screen.getByText('page one flag')).toBeInTheDocument())
    await user.click(screen.getAllByRole('checkbox')[1]!)
    expect(screen.getByText(/1 flag selected/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /next/i }))
    await waitFor(() => expect(screen.getByText('page two flag')).toBeInTheDocument())
    expect(screen.queryByText(/flag selected/i)).toBeNull()
  })

  it('per-row Assign reviewer opens the single-flag dialog', async () => {
    const user = userEvent.setup()
    server.use(
      queueHandler(),
      http.get('http://localhost/api/v1/submissions/:flagId/assignable-reviewers', () =>
        HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 }),
      ),
    )
    renderQueue()

    await waitFor(() => expect(screen.getByText('page one flag')).toBeInTheDocument())
    // Named by its row, so one button is distinguishable from the next while the label is hidden.
    await user.click(screen.getByRole('button', { name: 'Assign reviewer to: page one flag' }))
    // Single flag → the singular title, not "Assign reviewers to N flags".
    expect(await screen.findByRole('heading', { name: /^assign reviewers$/i })).toBeInTheDocument()
  })
})

describe('ReviewQueuePage — unassigned filter', () => {
  it('asks the server for unassigned flags only when the toggle is on', async () => {
    const user = userEvent.setup()
    server.use(queueHandler())
    renderQueue()
    await waitFor(() => expect(screen.getByText('page one flag')).toBeInTheDocument())

    await user.click(screen.getByRole('checkbox', { name: /unassigned only/i }))

    expect(await screen.findByText('nobody reviewing this')).toBeInTheDocument()
    expect(lastQueueUrl?.searchParams.get('unassigned')).toBe('true')
  })

  it('opens already filtered when the URL says so, so the tile can link straight here', async () => {
    server.use(queueHandler())
    renderQueue('/reviews/queue?unassigned=1')

    expect(await screen.findByRole('checkbox', { name: /unassigned only/i })).toBeChecked()
    await waitFor(() => expect(lastQueueUrl?.searchParams.get('unassigned')).toBe('true'))
  })

  it('reads the value, not just the key, so ?unassigned=0 leaves the filter off', async () => {
    server.use(queueHandler())
    renderQueue('/reviews/queue?unassigned=0')

    expect(await screen.findByText('page one flag')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /unassigned only/i })).not.toBeChecked()
    expect(lastQueueUrl?.searchParams.get('unassigned')).toBeNull()
  })

  it('clamps a hand-typed negative offset instead of letting the API 422 it', async () => {
    server.use(queueHandler())
    renderQueue('/reviews/queue?offset=-5')

    await waitFor(() => expect(lastQueueUrl?.searchParams.get('offset')).toBe('0'))
  })

  it('returns to the first page when the filter changes — page 2 of a narrower list is not page 2', async () => {
    const user = userEvent.setup()
    server.use(queueHandler())
    renderQueue()
    await user.click(await screen.findByRole('button', { name: /next/i }))
    await waitFor(() => expect(lastQueueUrl?.searchParams.get('offset')).toBe('20'))

    await user.click(screen.getByRole('checkbox', { name: /unassigned only/i }))

    await waitFor(() => expect(lastQueueUrl?.searchParams.get('unassigned')).toBe('true'))
    expect(lastQueueUrl?.searchParams.get('offset')).toBe('0')
  })
})
