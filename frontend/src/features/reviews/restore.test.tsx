import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { useUnassignReviewer } from './mutations'
import { ReviewsListPage } from './reviews-list-page'
import type { ReviewResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const REVIEW_ID = 'rev-0001-0000-0000-000000000000'

const unassignedReview: ReviewResponse = {
  id: REVIEW_ID,
  message_flag_id: 'flag-0001',
  reviewer_id: 'user-0002',
  reviewer_email: 'reviewer@example.com',
  assigned_by_id: 'user-0001',
  evaluation_id: 'eval-0001',
  status: 'approved',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  deleted_at: '2026-01-02T00:00:00Z',
  deleted_by_id: 'user-0001',
}

function reviewsHandler(onRequest?: (url: URL) => void, items: ReviewResponse[] = []) {
  return http.get('http://localhost/api/v1/reviews', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
  })
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewsListPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

function withClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

// The Undo action lives inside the toast options, and no <Toaster> is mounted in tests.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

describe('ReviewsListPage — unassigned view', () => {
  it('hides the toggle from a caller without reviews:delete', async () => {
    server.use(reviewsHandler())

    renderPage(['reviews:read'])

    await waitFor(() => expect(screen.getByText(/all reviewers/i)).toBeInTheDocument())
    expect(
      screen.queryByRole('combobox', { name: /which reviews to show/i }),
    ).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" sends deleted=true, newest first', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(reviewsHandler((url) => captured.push(url)))

    renderPage(['reviews:read', 'reviews:delete'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which reviews to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which reviews to show/i, /recently deleted/i)

    await waitFor(() => {
      const deleted = captured.findLast((u) => u.searchParams.get('deleted') === 'true')
      expect(deleted).toBeDefined()
      expect(deleted?.searchParams.get('order_by')).toBe('-deleted_at')
    })
  })

  it('re-assigns the reviewer through the Restore action', async () => {
    const user = userEvent.setup()
    let restored: string | null = null
    server.use(
      http.get('http://localhost/api/v1/reviews', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        const items = deleted && !restored ? [unassignedReview] : []
        return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/reviews/${REVIEW_ID}/restore`, () => {
        restored = REVIEW_ID
        return HttpResponse.json({ ...unassignedReview, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderPage(['reviews:read', 'reviews:delete'])

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /which reviews to show/i })).toBeInTheDocument(),
    )
    await chooseOption(user, /which reviews to show/i, /recently deleted/i)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /restore reviewer/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /restore reviewer/i }))

    await waitFor(() => expect(restored).toBe(REVIEW_ID))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /restore reviewer/i })).not.toBeInTheDocument(),
    )
  })
})

describe('undoing a reviewer unassign', () => {
  beforeEach(() => vi.mocked(toast.success).mockClear())

  it('fires once when Undo is double-clicked', async () => {
    const attempts: string[] = []
    server.use(
      http.delete(
        `http://localhost/api/v1/reviews/${REVIEW_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
      http.post(`http://localhost/api/v1/reviews/${REVIEW_ID}/restore`, () => {
        attempts.push(REVIEW_ID)
        return HttpResponse.json({ ...unassignedReview, deleted_at: null })
      }),
    )
    const { wrapper } = withClient()
    const { result } = renderHook(() => useUnassignReviewer(), { wrapper })

    await act(() => result.current.mutateAsync(REVIEW_ID))
    const undo = undoFromLastToast()
    await act(async () => {
      undo()
      undo()
    })

    await waitFor(() => expect(attempts).toEqual([REVIEW_ID]))
  })

  it('refreshes the queue and the candidate pool, not just the listing', async () => {
    // A restore re-disqualifies that reviewer from the assign picker and moves the
    // queue's completed count back — the mirror of what the unassign did.
    server.use(
      http.post(`http://localhost/api/v1/reviews/${REVIEW_ID}/restore`, () =>
        HttpResponse.json({ ...unassignedReview, deleted_at: null }),
      ),
      http.delete(
        `http://localhost/api/v1/reviews/${REVIEW_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useUnassignReviewer(), { wrapper })

    await act(() => result.current.mutateAsync(REVIEW_ID))
    spy.mockClear()
    await act(async () => undoFromLastToast()())

    for (const queryKey of [
      ['review-queue'],
      ['reviews'],
      ['submission'],
      ['assignable-reviewers'],
    ]) {
      await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey })))
    }
  })
})
