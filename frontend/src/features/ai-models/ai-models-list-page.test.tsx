import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AiModelsListPage } from './ai-models-list-page'
import type { AiModelResponse } from '@/lib/api/types'

function CreateRouteProbe() {
  return <div>create {useLocation().search}</div>
}

// Asymmetric on purpose: with input === output a swapped `modalitySummary` argument
// pair renders identically and the column's test proves nothing.
const visionModel: AiModelResponse = {
  id: 'model-0001-0000-0000-000000000000',
  name: 'Vision',
  model_alias: 'vision',
  provider: 'openai',
  input_modalities: ['text', 'image'],
  output_modalities: ['text'],
  provider_model_id: 'gpt-4o',
  labels: [],
  has_api_key: false,
  is_disabled: false,
  warmup_enabled: false,
  advanced_params_disabled: false,
  parameters: {},
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const imageOnlyModel: AiModelResponse = {
  ...visionModel,
  id: 'model-0002-0000-0000-000000000000',
  name: 'Painter',
  model_alias: 'painter',
  input_modalities: ['text'],
  output_modalities: ['image'],
}

// Two columns render an em dash when empty (the admin note and the labels), so a page- or
// row-wide dash query cannot tell them apart. Resolve the column by its header, then read that
// cell of the row.
function cellUnder(row: HTMLElement, header: string) {
  const headers = [...row.closest('table')!.querySelectorAll('thead th')]
  const index = headers.findIndex((th) => th.textContent?.trim() === header)
  if (index < 0) throw new Error(`no column headed "${header}"`)
  return row.querySelectorAll('td')[index] as HTMLElement
}

function renderList(path: string, items: AiModelResponse[] = []) {
  server.use(
    http.get('http://localhost/api/v1/ai-models', () =>
      HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['models:create', 'models:update'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/ai-models" element={<AiModelsListPage />} />
            <Route path="/ai-models/new" element={<CreateRouteProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('AiModelsListPage — add flow', () => {
  it('opens the chooser from New model and carries the kind into the create route', async () => {
    const user = userEvent.setup()
    renderList('/ai-models')

    await user.click(await screen.findByRole('button', { name: /new model/i }))
    await user.click(await screen.findByRole('button', { name: /Your own endpoint/ }))

    expect(await screen.findByText('create ?kind=custom')).toBeInTheDocument()
  })

  it('opens the chooser straight from ?new, so a bookmarked create URL lands on the choice', async () => {
    renderList('/ai-models?new')

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Provider API/ })).toBeInTheDocument()
  })

  it('closes the chooser on Cancel', async () => {
    const user = userEvent.setup()
    renderList('/ai-models?new')

    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Provider API/ })).not.toBeInTheDocument(),
    )
  })
})

describe('AiModelsListPage — admin note column', () => {
  it('lists the note, so engagements read off the table instead of one model at a time', async () => {
    renderList('/ai-models', [{ ...visionModel, description: 'Client Acme only.' }])

    // The cell, not the header: headers render over the loading skeleton, so asserting one
    // synchronously would pass before any row exists.
    expect(await screen.findByText('Client Acme only.')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Description' })).toBeInTheDocument()
  })

  it('renders an em dash for a note that is only whitespace', async () => {
    // New writes normalise a blank note to null at the edge, but rows written before that
    // still hold one, so the cell is what makes them read as unset rather than as a gap.
    renderList('/ai-models', [{ ...visionModel, description: '   ' }])

    const row = (await screen.findByText('Vision')).closest('tr') as HTMLElement
    expect(cellUnder(row, 'Description')).toHaveTextContent('—')
  })

  it('renders an em dash for a model with no note', async () => {
    renderList('/ai-models', [{ ...visionModel, description: null }])

    // Scoped to this column, not just to a cell: the labels column also dashes when empty, so a
    // page- or row-wide query would pass even if this column rendered blank.
    const row = (await screen.findByText('Vision')).closest('tr') as HTMLElement
    expect(cellUnder(row, 'Description')).toHaveTextContent('—')
  })
})

describe('AiModelsListPage — rows', () => {
  it("renders a row's directions in prompt → reply order", async () => {
    renderList('/ai-models', [visionModel])

    expect(await screen.findByText('Text, Image → Text')).toBeInTheDocument()
    // Negative: `capability_mismatch` is optional, so an unconditionally rendered badge would
    // otherwise pass every assertion above.
    expect(screen.queryByText(/unconfirmed/)).not.toBeInTheDocument()
  })

  it('reports a model that cannot reply in text as unusable', async () => {
    renderList('/ai-models', [imageOnlyModel])

    await screen.findByText('Painter')
    expect(screen.getByText('no text output')).toBeInTheDocument()
  })

  it("renders a row's labels as capped, titled badges in the labels column", async () => {
    renderList('/ai-models', [{ ...visionModel, labels: ['self-hosted', 'audited'] }])

    await screen.findByText('Vision')
    const row = (await screen.findByText('Vision')).closest('tr') as HTMLElement
    // Page-wide text would pass on bare text in any cell; the badge carries the full value in a
    // `title` because it truncates, so assert the element that does the truncating.
    const badges = cellUnder(row, 'Labels').querySelectorAll('[title]')
    expect([...badges].map((b) => b.getAttribute('title'))).toEqual(['self-hosted', 'audited'])
    expect(badges[0]).toHaveTextContent('self-hosted')
  })

  it('shows a dash for a model carrying no labels', async () => {
    renderList('/ai-models', [visionModel])

    // Scoped to the labels column: the admin-note column also dashes when empty, so a row-wide
    // query would pass on that one instead.
    const row = (await screen.findByText('Vision')).closest('tr') as HTMLElement
    expect(cellUnder(row, 'Labels')).toHaveTextContent('—')
  })

  it('marks a declaration the last health check could not confirm', async () => {
    renderList('/ai-models', [
      { ...visionModel, capability_mismatch: 'unsupported content type: image_url' },
    ])

    // Under Modality, not Status: the doubt is about the claim, and the model stays usable.
    const row = (await screen.findByText('Vision')).closest('tr') as HTMLElement
    expect(cellUnder(row, 'Modality')).toHaveTextContent('image input unconfirmed')
    expect(cellUnder(row, 'Status')).toHaveTextContent('enabled')
    // `toHaveTextContent` is a substring match, so Status could carry both without this.
    expect(cellUnder(row, 'Status')).not.toHaveTextContent(/unconfirmed/i)
    // Marker only — the 255-char provider text must not ride into a row sweep via `title`.
    expect(cellUnder(row, 'Modality').querySelector('[title]')).toBeNull()
  })

  it('reports an enabled text model as usable', async () => {
    renderList('/ai-models', [visionModel])

    await screen.findByText('Vision')
    expect(screen.getByText('enabled')).toBeInTheDocument()
  })
})

describe('AiModelsListPage — deleted view', () => {
  const tombstone: AiModelResponse = {
    ...visionModel,
    id: 'model-0003-0000-0000-000000000000',
    name: 'Retired',
    model_alias: 'retired',
    deleted_at: '2026-01-02T00:00:00Z',
    deleted_by_id: 'user-0001-0000-0000-000000000000',
  }

  function renderWithDelete(permissions: string[] = ['models:delete']) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(permissions)
    render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/ai-models']}>
            <Routes>
              <Route path="/ai-models" element={<AiModelsListPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </Wrapper>,
    )
  }

  it('hides the toggle from a caller without models:delete', async () => {
    server.use(
      http.get('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json({ items: [visionModel], total: 1, limit: 20, offset: 0 }),
      ),
    )

    renderWithDelete(['models:read'])

    await screen.findByText('Vision')
    expect(screen.queryByRole('combobox', { name: /model rows/i })).not.toBeInTheDocument()
  })

  it('selecting "Recently deleted" asks the server for tombstones', async () => {
    const user = userEvent.setup()
    const captured: URL[] = []
    server.use(
      http.get('http://localhost/api/v1/ai-models', ({ request }) => {
        const url = new URL(request.url)
        captured.push(url)
        const deleted = url.searchParams.get('deleted') === 'true'
        return HttpResponse.json({
          items: deleted ? [tombstone] : [visionModel],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }),
    )

    renderWithDelete()
    await screen.findByText('Vision')
    await chooseOption(user, /model rows/i, /recently deleted/i)

    await screen.findByText('Retired')
    expect(captured.some((u) => u.searchParams.get('deleted') === 'true')).toBe(true)
  })

  it('restores a row and drops it from the deleted list', async () => {
    const user = userEvent.setup()
    let restored = false
    server.use(
      http.get('http://localhost/api/v1/ai-models', ({ request }) => {
        const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
        const items = deleted && !restored ? [tombstone] : []
        return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 })
      }),
      http.post(`http://localhost/api/v1/ai-models/${tombstone.id}/restore`, () => {
        restored = true
        return HttpResponse.json({ ...tombstone, deleted_at: null, deleted_by_id: null })
      }),
    )

    renderWithDelete()
    await chooseOption(user, /model rows/i, /recently deleted/i)
    await user.click(await screen.findByRole('button', { name: /restore model: retired/i }))

    // Not just the request: the row has to leave the list, which only happens if the
    // mutation invalidated the query behind it.
    await waitFor(() => expect(screen.queryByText('Retired')).not.toBeInTheDocument())
    expect(restored).toBe(true)
  })
})
