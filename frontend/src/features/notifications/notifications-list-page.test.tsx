import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { NotificationsListPage } from './notifications-list-page'

const UNREAD = {
  id: 'n1',
  name: 'Evaluation approved',
  description: 'Nice work',
  read: false,
  read_at: null,
  object_type: 'evaluation',
  object_id: 'e1',
  user_id: 'u1',
  created_at: '2026-07-22T10:00:00Z',
  updated_at: '2026-07-22T10:00:00Z',
}
const READ = {
  ...UNREAD,
  id: 'n2',
  name: 'Evaluation rejected',
  read: true,
  read_at: '2026-07-22T11:00:00Z',
}

function listHandler(onRequest?: (url: URL) => void) {
  return http.get('http://localhost/api/v1/notifications', ({ request }) => {
    onRequest?.(new URL(request.url))
    return HttpResponse.json({ items: [UNREAD, READ], total: 2, limit: 20, offset: 0 })
  })
}

function renderPage(perms: string[] = ['notifications:read', 'notifications:update']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/notifications']}>
          <NotificationsListPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('NotificationsListPage', () => {
  it('renders the caller notifications', async () => {
    server.use(listHandler())

    renderPage()

    expect(await screen.findByText('Evaluation approved')).toBeInTheDocument()
    expect(screen.getByText('Evaluation rejected')).toBeInTheDocument()
    expect(screen.getByLabelText('unread')).toBeInTheDocument()
  })

  // The object badge is the row's only route to what the notification is about, and this table has
  // no detail view or expander - so unlike the other columns it may never be dropped for width.
  it('keeps the object link reachable at every width', async () => {
    server.use(listHandler())

    renderPage()

    const badge = (await screen.findAllByText('evaluation'))[0]!
    const cell = badge.closest('td')!
    expect(cell.className).not.toMatch(/hidden/)
    expect(badge.closest('a')).toHaveAttribute('href', expect.stringContaining('e1'))
  })

  it('the Unread filter requests read=false', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(listHandler((url) => captured.push(url)))

    renderPage()

    await screen.findByText('Evaluation approved')
    await user.click(screen.getByRole('button', { name: /^unread$/i }))

    await waitFor(() =>
      expect(captured.some((u) => u.searchParams.get('read') === 'false')).toBe(true),
    )
  })

  it('Mark all read posts an empty id list', async () => {
    const user = userEvent.setup()
    let body: unknown
    server.use(
      listHandler(),
      http.post('http://localhost/api/v1/notifications/mark', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ updated: 1 })
      }),
    )

    renderPage()

    await screen.findByText('Evaluation approved')
    await user.click(screen.getByRole('button', { name: /mark all read/i }))

    await waitFor(() => expect(body).toEqual({ ids: [], read: true }))
  })

  it('a row Mark read posts that notification id', async () => {
    const user = userEvent.setup()
    let body: unknown
    server.use(
      listHandler(),
      http.post('http://localhost/api/v1/notifications/mark', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ updated: 1 })
      }),
    )

    renderPage()

    await screen.findByText('Evaluation approved')
    await user.click(screen.getByRole('button', { name: /^mark read$/i }))

    await waitFor(() => expect(body).toEqual({ ids: ['n1'], read: true }))
  })

  it('hides mark actions without notifications:update', async () => {
    server.use(listHandler())

    renderPage(['notifications:read'])

    await screen.findByText('Evaluation approved')
    expect(screen.queryByRole('button', { name: /mark all read/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^mark read$/i })).not.toBeInTheDocument()
  })
})
