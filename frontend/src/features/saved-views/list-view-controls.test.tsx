import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ListViewControls, ResetViewButton } from './list-view-controls'
import type { ListViewState } from './use-list-view-state'
import type { Column } from '@/components/shared/data-table'

type Row = { id: string; name: string }
const columns: Column<Row>[] = [
  { id: 'name', header: 'Name', cell: (r) => r.name },
  { header: 'Actions', cell: () => null },
]

function makeView(isDirty = false): ListViewState<Record<string, unknown>> {
  return {
    search: '',
    searchDraft: '',
    filters: {},
    orderBy: undefined,
    hiddenColumns: [],
    offset: 0,
    isDirty,
    setSearchDraft: vi.fn(),
    commitSearch: vi.fn(),
    setFilter: vi.fn(),
    setOrderBy: vi.fn(),
    setOffset: vi.fn(),
    setHiddenColumns: vi.fn(),
    serialize: vi.fn(() => ({})),
    apply: vi.fn(),
    reset: vi.fn(),
  }
}

function renderControls(
  perms: string[],
  {
    cols = columns,
    view = makeView(),
  }: { cols?: Column<Row>[]; view?: ListViewState<Record<string, unknown>> } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Auth = authWrapper(perms)
  return render(
    <QueryClientProvider client={qc}>
      <Auth>
        <MemoryRouter>
          <ListViewControls resource="evaluations" view={view} columns={cols} />
        </MemoryRouter>
      </Auth>
    </QueryClientProvider>,
  )
}

describe('ListViewControls', () => {
  it('shows the saved-views menu when the caller has saved_views:read', () => {
    server.use(
      http.get('http://localhost/api/v1/saved-views', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderControls(['saved_views:read'])
    expect(screen.getByRole('button', { name: 'Saved views' })).toBeInTheDocument()
  })

  it('hides the saved-views menu without the permission', () => {
    renderControls([])
    expect(screen.queryByRole('button', { name: 'Saved views' })).toBeNull()
  })

  it('shows the columns menu only when a column has an id', () => {
    renderControls([])
    expect(screen.getByRole('button', { name: 'Toggle columns' })).toBeInTheDocument()
  })

  it('hides the columns menu when no column is toggleable', () => {
    renderControls([], { cols: [{ header: 'Actions', cell: () => null }] })
    expect(screen.queryByRole('button', { name: 'Toggle columns' })).toBeNull()
  })

  it('enables the reset button when the view is dirty and resets on click', async () => {
    const user = userEvent.setup()
    const view = makeView(true)
    render(<ResetViewButton view={view} />)
    const reset = screen.getByRole('button', { name: 'Reset view' })
    expect(reset).toBeEnabled()
    await user.click(reset)
    expect(view.reset).toHaveBeenCalledTimes(1)
  })

  it('keeps the reset button present but disabled when the view is clean (no layout shift)', async () => {
    const user = userEvent.setup()
    const view = makeView(false)
    render(<ResetViewButton view={view} />)
    const reset = screen.getByRole('button', { name: 'Reset view' })
    expect(reset).toBeDisabled()
    // disabled → clicking is a no-op.
    await user.click(reset)
    expect(view.reset).not.toHaveBeenCalled()
  })
})
