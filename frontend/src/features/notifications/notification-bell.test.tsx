import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { NotificationBell } from './notification-bell'

const NOTE = {
  id: 'n1',
  name: 'Evaluation approved',
  description: null,
  read: false,
  read_at: null,
  object_type: 'evaluation',
  object_id: 'e1',
  user_id: 'u1',
  created_at: '2026-07-22T10:00:00Z',
  updated_at: '2026-07-22T10:00:00Z',
}

type Page = { items: Array<typeof NOTE>; total: number; limit: number; offset: number }

function listHandler(body: (url: URL) => Page) {
  return http.get('http://localhost/api/v1/notifications', ({ request }) =>
    HttpResponse.json(body(new URL(request.url))),
  )
}

function renderBell(perms: string[] = ['notifications:read', 'notifications:update']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <NotificationBell />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('NotificationBell', () => {
  it('shows the unread count badge', async () => {
    server.use(listHandler(() => ({ items: [NOTE], total: 3, limit: 20, offset: 0 })))

    renderBell()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /3 unread/i })).toBeInTheDocument(),
    )
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('caps the badge at 9+', async () => {
    server.use(listHandler(() => ({ items: [NOTE], total: 42, limit: 20, offset: 0 })))

    renderBell()

    await waitFor(() => expect(screen.getByText('9+')).toBeInTheDocument())
  })

  it('opens a preview with recent notifications and a See all link', async () => {
    const user = userEvent.setup()
    server.use(listHandler(() => ({ items: [NOTE], total: 1, limit: 20, offset: 0 })))

    renderBell()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /1 unread/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /1 unread/i }))

    expect(await screen.findByText('Evaluation approved')).toBeInTheDocument()
    // The item deep-links to its object; "See all" goes to the full view.
    expect(screen.getByRole('link', { name: /evaluation approved/i })).toHaveAttribute(
      'href',
      '/evaluations/e1',
    )
    expect(screen.getByRole('link', { name: /see all/i })).toHaveAttribute('href', '/notifications')
  })

  it('marks all read, clearing the badge', async () => {
    const user = userEvent.setup()
    let marked = false
    server.use(
      listHandler(() => ({ items: [NOTE], total: marked ? 0 : 2, limit: 20, offset: 0 })),
      http.post('http://localhost/api/v1/notifications/mark', async () => {
        marked = true
        return HttpResponse.json({ updated: 2 })
      }),
    )

    renderBell()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /2 unread/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /2 unread/i }))
    await user.click(await screen.findByRole('button', { name: /mark all read/i }))

    await waitFor(() => expect(screen.queryByText('2')).not.toBeInTheDocument())
  })

  it('hides Mark all read without notifications:update', async () => {
    const user = userEvent.setup()
    server.use(listHandler(() => ({ items: [NOTE], total: 2, limit: 20, offset: 0 })))

    renderBell(['notifications:read'])

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /2 unread/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /2 unread/i }))

    expect(await screen.findByText('Evaluation approved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /mark all read/i })).not.toBeInTheDocument()
  })
})
