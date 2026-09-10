import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AuditLogsListPage } from './audit-logs-list-page'
import type { AuditLogResponse } from '@/lib/api/types'

const mutationRow: AuditLogResponse = {
  id: 'a0000001-0000-0000-0000-000000000000',
  actor_id: 'u0000001-0000-0000-0000-000000000000',
  actor_email: 'admin@test.com',
  action: 'evaluation_group.publish',
  object_type: 'evaluation_group',
  object_id: 'g0000001-0000-0000-0000-000000000000',
  before: { status: 'approved' },
  after: { status: 'published' },
  context: {},
  request_id: 'req-1',
  created_at: '2026-05-05T10:00:00Z',
}

const systemRow: AuditLogResponse = {
  id: 'a0000002-0000-0000-0000-000000000000',
  actor_id: null,
  actor_email: null,
  action: 'data.read',
  object_type: 'export',
  object_id: 'e0000001-0000-0000-0000-000000000000',
  before: null,
  after: null,
  context: { route: '/exports' },
  request_id: 'req-2',
  created_at: '2026-05-05T09:00:00Z',
}

// Records the query string of the last request so filter wiring can be asserted.
function auditHandler(rows: AuditLogResponse[], seen: { url?: string }) {
  return http.get('http://localhost/api/v1/audit-logs', ({ request }) => {
    seen.url = new URL(request.url).search
    return HttpResponse.json({ items: rows, total: rows.length, limit: 20, offset: 0 })
  })
}

function renderPage(rows: AuditLogResponse[] = [mutationRow, systemRow]) {
  const seen: { url?: string } = {}
  server.use(auditHandler(rows, seen))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['audit:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AuditLogsListPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
  return seen
}

describe('AuditLogsListPage', () => {
  it('renders audit entries with humanized action and actor', async () => {
    renderPage()
    // Twice on purpose: the Actor column, plus the copy folded under the action for the widths
    // where that column is hidden. Only one is displayed at a time.
    await waitFor(() => expect(screen.getAllByText('admin@test.com')).toHaveLength(2))
    expect(screen.getByText('Evaluation group · Publish')).toBeInTheDocument()
    // object_type is title-cased to match the Action column (evaluation_group -> "Evaluation group")
    expect(screen.getByText('Evaluation group')).toBeInTheDocument()
  })

  // The Action column carries no `hideBelow`, and the actor rides along with it: this table has no
  // detail route, so anything dropped on a phone is unreachable rather than one tap away.
  it('keeps the action and the actor in the row at every width', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Evaluation group · Publish')).toBeInTheDocument())

    const actionCell = screen.getByText('Evaluation group · Publish').closest('td')!
    expect(actionCell.className).not.toMatch(/hidden/)
    expect(actionCell).toHaveTextContent('admin@test.com')
  })

  it('shows the empty state when no entries match', async () => {
    renderPage([])
    await waitFor(() =>
      expect(screen.getByText('No audit entries match these filters.')).toBeInTheDocument(),
    )
    expect(screen.getByText('Widen the date range or clear the action filter.')).toBeInTheDocument()
  })

  it('shows "— system —" for an actorless (system) entry', async () => {
    renderPage()
    await waitFor(() => expect(screen.getAllByText('— system —')).toHaveLength(2))
  })

  it('sends the picked action and date as query filters', async () => {
    const seen = renderPage()
    await waitFor(() => expect(screen.getByText('Evaluation group · Publish')).toBeInTheDocument())

    await chooseOption(userEvent.setup(), /^Action$/, /^Evaluation group · Publish$/)
    await waitFor(() => expect(seen.url).toContain('action=evaluation_group.publish'))

    // date field alone (time defaults to 00:00 / 23:59) still sends the range bound
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-05-05' } })
    await waitFor(() => expect(seen.url).toContain('created_from='))

    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-05-06' } })
    await waitFor(() => expect(seen.url).toContain('created_to='))
  })

  it('toggles the sort order_by when the Time header is clicked', async () => {
    const seen = renderPage()
    // default sort is newest-first
    await waitFor(() => expect(seen.url).toContain('order_by=-created_at'))

    fireEvent.click(screen.getByRole('button', { name: /time/i }))
    await waitFor(() => expect(seen.url).toContain('order_by=created_at'))
  })

  it('reveals before/after on row expand', async () => {
    renderPage()
    await waitFor(() => expect(screen.getAllByText('admin@test.com')).not.toHaveLength(0))
    expect(screen.queryByText('Before')).toBeNull()

    fireEvent.click(screen.getAllByRole('button', { name: /expand row/i })[0]!)

    expect(screen.getByText('Before')).toBeInTheDocument()
    expect(screen.getByText('After')).toBeInTheDocument()
    expect(screen.getByText((t) => t.includes('"published"'))).toBeInTheDocument()
    expect(screen.getByText(mutationRow.object_id!)).toBeInTheDocument()
  })
})
