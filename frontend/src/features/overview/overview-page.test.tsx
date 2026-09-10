import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { OverviewPage } from './overview-page'

function counts() {
  return [
    http.get('http://localhost/api/v1/reviews', () =>
      HttpResponse.json({ items: [], total: 3, limit: 1, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/message-flags', ({ request }) => {
      const status = new URL(request.url).searchParams.get('status')
      return HttpResponse.json({
        items: [],
        total: status === 'pending' ? 4 : 12,
        limit: 1,
        offset: 0,
      })
    }),
    http.get('http://localhost/api/v1/review-queue', () =>
      HttpResponse.json({ items: [], total: 7, limit: 1, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/evaluation-groups', () =>
      HttpResponse.json({ items: [], total: 2, limit: 1, offset: 0 }),
    ),
  ]
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <OverviewPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('OverviewPage — persona panels', () => {
  it('greets the user and always shows the All sections grid', async () => {
    server.use(...counts())
    renderPage(['models:read'])
    expect(screen.getByText(/welcome back, a@b\.c/i)).toBeInTheDocument()
    expect(screen.getByText('All sections')).toBeInTheDocument()
    expect(screen.getByText('AI Models')).toBeInTheDocument()
  })

  it('shows the reviewer panel with the pending count for reviews:update', async () => {
    server.use(...counts())
    renderPage(['reviews:read', 'reviews:update'])
    expect(screen.getByText('To review')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('3')).toBeInTheDocument())
  })

  it('shows the red-teamer panel with flag total and pending context', async () => {
    server.use(...counts())
    renderPage(['flags:read', 'flags:create'])
    expect(screen.getByText('Your flags')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('12')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText(/4 awaiting verdict/i)).toBeInTheDocument())
  })

  it('shows the queue panel for reviews:create', async () => {
    server.use(...counts())
    renderPage(['reviews:read', 'reviews:create'])
    expect(screen.getByText('Review queue')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument())
  })

  it('shows the groups-to-approve panel for evaluation_groups:update', async () => {
    server.use(...counts())
    renderPage(['evaluation_groups:read', 'evaluation_groups:update'])
    expect(screen.getByText('Groups to approve')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('2')).toBeInTheDocument())
  })

  it('shows no persona panels for a permission set that matches none', () => {
    server.use(...counts())
    renderPage(['models:read'])
    expect(screen.queryByText('To review')).toBeNull()
    expect(screen.queryByText('Your flags')).toBeNull()
    expect(screen.queryByText('Review queue')).toBeNull()
    expect(screen.queryByText('Groups to approve')).toBeNull()
  })
})
