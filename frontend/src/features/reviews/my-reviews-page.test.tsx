import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MyReviewsPage } from './my-reviews-page'

const EVAL_ID = 'eval-mine-0000-0000-000000000000'
const FLAG_ID = 'flag-mine-0000-0000-000000000000'

function reviewsHandler(status: 'pending' | 'approved') {
  return http.get('http://localhost/api/v1/reviews', ({ request }) => {
    const url = new URL(request.url)
    // Echo the requested status so the test can assert the filter is applied.
    const filtered = url.searchParams.get('status')
    return HttpResponse.json({
      items:
        filtered && filtered !== status
          ? []
          : [
              {
                id: 'rev-mine-0000-0000-000000000000',
                reviewer_id: 'user-self',
                assigned_by_id: 'user-0001',
                evaluation_id: EVAL_ID,
                message_flag_id: FLAG_ID,
                status,
                created_at: '2026-01-01T00:00:00Z',
                updated_at: '2026-01-01T00:00:00Z',
              },
            ],
      total: 1,
      limit: 20,
      offset: 0,
    })
  })
}

function renderMine() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['reviews:read', 'reviews:update'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/reviews/mine']}>
          <Routes>
            <Route path="/reviews/mine" element={<MyReviewsPage />} />
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

describe('MyReviewsPage', () => {
  it('lists my pending reviews by default', async () => {
    server.use(reviewsHandler('pending'))
    renderMine()
    // Exact-case 'pending' is the status pill, not the capital-P toggle button.
    await waitFor(() => expect(screen.getByText('pending')).toBeInTheDocument())
    expect(screen.getByRole('columnheader', { name: /verdict/i })).toBeInTheDocument()
  })

  it('clicking a row opens that submission detail', async () => {
    const user = userEvent.setup()
    server.use(reviewsHandler('pending'))
    renderMine()
    await waitFor(() => expect(screen.getByText('pending')).toBeInTheDocument())
    await user.click(screen.getByText('pending'))
    await waitFor(() => expect(screen.getByText('SUBMISSION DETAIL')).toBeInTheDocument())
  })

  it('the All mine toggle drops the pending filter', async () => {
    const user = userEvent.setup()
    server.use(reviewsHandler('approved'))
    renderMine()
    // Default (pending) filter yields nothing for an approved-only dataset.
    await waitFor(() => expect(screen.getByText(/nothing assigned to you/i)).toBeInTheDocument())
    await user.click(screen.getByRole('radio', { name: /all mine/i }))
    await waitFor(() => expect(screen.getByText('approved')).toBeInTheDocument())
  })
})
