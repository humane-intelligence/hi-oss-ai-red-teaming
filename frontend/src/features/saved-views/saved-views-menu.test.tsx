import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { SavedViewsMenu } from './saved-views-menu'
import type { ListViewState } from './use-list-view-state'
import type { SavedViewResponse } from '@/lib/api/types'

const VIEW_A: SavedViewResponse = {
  id: 'sv-1',
  resource: 'evaluations',
  name: 'Newest first',
  state: { order_by: '-created_at', filters: {}, hidden_columns: [], search: null },
  created_by_id: 'u1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const SERIALIZED = {
  order_by: 'title',
  filters: { status: 'draft' },
  hidden_columns: [],
  search: null,
}

function makeView(): ListViewState<Record<string, unknown>> {
  return {
    search: '',
    searchDraft: '',
    filters: {},
    orderBy: undefined,
    hiddenColumns: [],
    offset: 0,
    isDirty: false,
    setSearchDraft: vi.fn(),
    commitSearch: vi.fn(),
    setFilter: vi.fn(),
    setOrderBy: vi.fn(),
    setOffset: vi.fn(),
    setHiddenColumns: vi.fn(),
    serialize: vi.fn(() => SERIALIZED),
    apply: vi.fn(),
    reset: vi.fn(),
  }
}

function renderMenu(view: ListViewState<Record<string, unknown>>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Auth = authWrapper([
    'saved_views:read',
    'saved_views:create',
    'saved_views:update',
    'saved_views:delete',
  ])
  return render(
    <QueryClientProvider client={qc}>
      <Auth>
        <MemoryRouter>
          <SavedViewsMenu resource="evaluations" view={view} />
        </MemoryRouter>
      </Auth>
    </QueryClientProvider>,
  )
}

function listHandler(items: SavedViewResponse[]) {
  return http.get('http://localhost/api/v1/saved-views', () =>
    HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 }),
  )
}

describe('SavedViewsMenu', () => {
  it('lists the caller saved views for the resource', async () => {
    server.use(listHandler([VIEW_A]))
    const user = userEvent.setup()
    renderMenu(makeView())
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await waitFor(() => expect(screen.getByText('Newest first')).toBeInTheDocument())
  })

  it('applies a view state on click', async () => {
    server.use(listHandler([VIEW_A]))
    const user = userEvent.setup()
    const view = makeView()
    renderMenu(view)
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await user.click(await screen.findByText('Newest first'))
    expect(view.apply).toHaveBeenCalledWith(VIEW_A.state)
  })

  it('saves the current state as a new view', async () => {
    let body: unknown
    server.use(
      listHandler([]),
      http.post('http://localhost/api/v1/saved-views', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ...VIEW_A, name: 'My view', state: SERIALIZED }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderMenu(makeView())
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await user.click(screen.getByRole('button', { name: /save current view/i }))
    await user.type(screen.getByLabelText('Name'), 'My view')
    await user.click(screen.getByRole('button', { name: 'Save view' }))
    await waitFor(() =>
      expect(body).toEqual({ resource: 'evaluations', name: 'My view', state: SERIALIZED }),
    )
  })

  it('renames an existing view', async () => {
    let body: unknown
    server.use(
      listHandler([VIEW_A]),
      http.patch('http://localhost/api/v1/saved-views/sv-1', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ...VIEW_A, name: 'Renamed' })
      }),
    )
    const user = userEvent.setup()
    renderMenu(makeView())
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await user.click(await screen.findByRole('button', { name: 'Rename Newest first' }))
    const input = screen.getByLabelText('Name')
    await user.clear(input)
    await user.type(input, 'Renamed')
    await user.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() => expect(body).toEqual({ name: 'Renamed' }))
  })

  it('deletes a view after confirmation', async () => {
    let deleted = false
    server.use(
      listHandler([VIEW_A]),
      http.delete('http://localhost/api/v1/saved-views/sv-1', () => {
        deleted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderMenu(makeView())
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await user.click(await screen.findByRole('button', { name: 'Delete Newest first' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deleted).toBe(true))
  })

  it('keeps the dialog open when the delete fails', async () => {
    let attempted = false
    server.use(
      listHandler([VIEW_A]),
      http.delete('http://localhost/api/v1/saved-views/sv-1', () => {
        attempted = true
        return new HttpResponse(null, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    renderMenu(makeView())
    await user.click(screen.getByRole('button', { name: 'Saved views' }))
    await user.click(await screen.findByRole('button', { name: 'Delete Newest first' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(attempted).toBe(true))
    // onSuccess never runs on failure, so the confirm dialog stays open for a retry.
    expect(screen.getByText('Delete saved view')).toBeInTheDocument()
  })
})
