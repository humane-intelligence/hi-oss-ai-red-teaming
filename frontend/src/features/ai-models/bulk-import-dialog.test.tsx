import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { BulkImportModelsDialog, BulkSetApiKeysDialog } from './bulk-import-dialog'
import { AiModelsListPage } from './ai-models-list-page'
import type { AiModelResponse, BulkModelResponse } from '@/lib/api/types'

const MODEL: AiModelResponse = {
  id: 'model-0001',
  name: 'Claude Sonnet 4.6',
  model_alias: 'claude-sonnet-4-6',
  provider: 'anthropic',
  input_modalities: ['text'],
  output_modalities: ['text'],
  provider_model_id: 'claude-sonnet-4-6',
  labels: [],
  has_api_key: true,
  is_disabled: false,
  warmup_enabled: false,
  advanced_params_disabled: false,
  parameters: {},
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const MODELS_JSON = JSON.stringify([
  {
    name: 'Claude Sonnet 4.6',
    model_alias: 'claude-sonnet-4-6',
    provider: 'anthropic',
    provider_model_id: 'claude-sonnet-4-6',
    api_key: 'sk-ant-test',
  },
])

const KEYS_JSON = JSON.stringify([{ name: 'Claude Sonnet 4.6', api_key: 'sk-ant-test' }])

function okResponse(dry_run: boolean): BulkModelResponse {
  return {
    dry_run,
    total: 1,
    succeeded: 1,
    failed: 0,
    results: [{ row_key: '0', status: 'ok', data: MODEL }],
  }
}

async function pasteInto(user: ReturnType<typeof userEvent.setup>, json: string) {
  const textarea = screen.getByLabelText(/rows/i)
  await user.click(textarea)
  await user.paste(json)
}

function renderModelsDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['models:create'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <BulkImportModelsDialog open onOpenChange={() => {}} />
      </QueryClientProvider>
    </Wrapper>,
  )
}

function renderKeysDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['models:update'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <BulkSetApiKeysDialog open onOpenChange={() => {}} />
      </QueryClientProvider>
    </Wrapper>,
  )
}

function renderListPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  server.use(
    http.get('http://localhost/api/v1/ai-models', () =>
      HttpResponse.json({ items: [MODEL], total: 1, limit: 20, offset: 0 }),
    ),
  )
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

describe('BulkImportModelsDialog', () => {
  it('Import sends one row with row_key + data + dry_run:false', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []
    server.use(
      http.post('http://localhost/api/v1/ai-models/bulk', async ({ request }) => {
        captured.push(await request.json())
        return HttpResponse.json(okResponse(false))
      }),
    )

    renderModelsDialog()
    await pasteInto(user, MODELS_JSON)
    await user.click(screen.getByRole('button', { name: /^import/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    const body = captured[0] as {
      rows: { row_key: string; data: { name: string; provider: string; api_key: string } }[]
      dry_run: boolean
    }
    expect(body.dry_run).toBe(false)
    expect(body.rows).toHaveLength(1)
    expect(body.rows[0]?.row_key).toBe('0')
    expect(body.rows[0]?.data.name).toBe('Claude Sonnet 4.6')
    expect(body.rows[0]?.data.provider).toBe('anthropic')
    expect(body.rows[0]?.data.api_key).toBe('sk-ant-test')

    await waitFor(() => expect(screen.getByText(/1 of 1 succeeded/i)).toBeInTheDocument())
  })

  it('Preview sends dry_run:true and shows a preview without committing', async () => {
    const user = userEvent.setup()
    const captured: BulkModelResponse[] = []
    server.use(
      http.post('http://localhost/api/v1/ai-models/bulk', async ({ request }) => {
        const body = (await request.json()) as { dry_run: boolean }
        captured.push(okResponse(body.dry_run))
        return HttpResponse.json(okResponse(body.dry_run))
      }),
    )

    renderModelsDialog()
    await pasteInto(user, MODELS_JSON)
    await user.click(screen.getByRole('button', { name: /^preview/i }))

    await waitFor(() => expect(screen.getByText(/nothing saved yet/i)).toBeInTheDocument())
    expect(screen.getByText(/1 of 1 rows would succeed/i)).toBeInTheDocument()
    expect(captured).toHaveLength(1)
    expect(captured[0]?.dry_run).toBe(true)
  })

  it('surfaces a per-row failure in the summary', async () => {
    const user = userEvent.setup()
    server.use(
      http.post('http://localhost/api/v1/ai-models/bulk', async () => {
        const response: BulkModelResponse = {
          dry_run: false,
          total: 1,
          succeeded: 0,
          failed: 1,
          results: [
            {
              row_key: '0',
              status: 'failed',
              error: {
                type: 'about:blank',
                title: 'Conflict',
                status: 409,
                detail: 'model_alias already exists',
              },
            },
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderModelsDialog()
    await pasteInto(user, MODELS_JSON)
    await user.click(screen.getByRole('button', { name: /^import/i }))

    await waitFor(() => expect(screen.getByText(/1 failed/i)).toBeInTheDocument())
    expect(screen.getByText(/model_alias already exists/i)).toBeInTheDocument()
    // The failed row is labelled by its submitted identity, not a bare index.
    expect(screen.getByText(/Claude Sonnet 4.6 \(Anthropic\)/)).toBeInTheDocument()
  })

  it('disables the actions and reports invalid JSON', async () => {
    const user = userEvent.setup()
    renderModelsDialog()
    await pasteInto(user, '{ not an array }')

    expect(screen.getByRole('button', { name: /^import/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /^preview/i })).toBeDisabled()
    expect(screen.getByText(/invalid json/i)).toBeInTheDocument()
  })

  it('names the offending row when a config carries a retired modality field', async () => {
    // The backend rejects these by name, but as one envelope-level 422 for the whole
    // batch — so a config file from the previous cycle would otherwise fail with
    // "Request body failed validation." and no row number.
    const user = userEvent.setup()
    renderModelsDialog()
    await pasteInto(
      user,
      JSON.stringify([
        { name: 'A', model_alias: 'a', provider: 'openai', provider_model_id: 'x' },
        {
          name: 'B',
          model_alias: 'b',
          provider: 'openai',
          provider_model_id: 'x',
          modality: 'text_to_image',
        },
      ]),
    )

    expect(screen.getByRole('button', { name: /^import/i })).toBeDisabled()
    expect(screen.getByText(/Row 2: modality — retired/)).toBeInTheDocument()
  })

  it('rejects an input set that omits text, per row', async () => {
    const user = userEvent.setup()
    renderModelsDialog()
    await pasteInto(
      user,
      JSON.stringify([
        {
          name: 'B',
          model_alias: 'b',
          provider: 'openai',
          provider_model_id: 'x',
          input_modalities: ['image'],
        },
      ]),
    )

    expect(screen.getByText(/Row 1: input_modalities — must include 'text'/)).toBeInTheDocument()
  })

  it('fills the textarea from an uploaded .json file', async () => {
    renderModelsDialog()
    const file = new File([MODELS_JSON], 'models.json', { type: 'application/json' })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(screen.getByText(/1 row\(s\) parsed/i)).toBeInTheDocument())
    expect(screen.getByLabelText(/rows/i)).toHaveValue(MODELS_JSON)
  })

  it('keeps a 500 off the inline surface and shows no false success', async () => {
    const user = userEvent.setup()
    server.use(
      http.post('http://localhost/api/v1/ai-models/bulk', () =>
        HttpResponse.json({ title: 'Server Error', status: 500 }, { status: 500 }),
      ),
    )

    renderModelsDialog()
    await pasteInto(user, MODELS_JSON)
    await user.click(screen.getByRole('button', { name: /^import/i }))

    // The request settles (the button leaves its "Importing…" state) ...
    await waitFor(() => expect(screen.getByRole('button', { name: /^import/i })).toBeEnabled())
    // ... but a non-field error stays off the inline surface (it rides the
    // global toast) and never renders a success/preview summary.
    expect(screen.queryByText(/something went wrong/i)).toBeNull()
    expect(screen.queryByText(/succeeded/i)).toBeNull()
    expect(screen.queryByText(/would succeed/i)).toBeNull()
  })
})

describe('BulkSetApiKeysDialog', () => {
  it('Import posts name + api_key rows to the keys endpoint', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []
    server.use(
      http.post('http://localhost/api/v1/ai-models/api-keys/bulk', async ({ request }) => {
        captured.push(await request.json())
        return HttpResponse.json(okResponse(false))
      }),
    )

    renderKeysDialog()
    await pasteInto(user, KEYS_JSON)
    await user.click(screen.getByRole('button', { name: /^import/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    const body = captured[0] as {
      rows: { data: { name: string; api_key: string } }[]
      dry_run: boolean
    }
    expect(body.dry_run).toBe(false)
    expect(body.rows[0]?.data.name).toBe('Claude Sonnet 4.6')
    expect(body.rows[0]?.data.api_key).toBe('sk-ant-test')
  })
})

describe('AiModelsListPage — bulk action gates', () => {
  it('shows Bulk import + New model with models:create', async () => {
    renderListPage(['models:read', 'models:create'])
    await waitFor(() => expect(screen.getByText('Claude Sonnet 4.6')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /bulk import/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /new model/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /bulk set keys/i })).toBeNull()
  })

  it('shows Bulk set keys with models:update', async () => {
    renderListPage(['models:read', 'models:update'])
    await waitFor(() => expect(screen.getByText('Claude Sonnet 4.6')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /bulk set keys/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /bulk import/i })).toBeNull()
  })

  it('hides all write actions with models:read only', async () => {
    renderListPage(['models:read'])
    await waitFor(() => expect(screen.getByText('Claude Sonnet 4.6')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /bulk import/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /bulk set keys/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /new model/i })).toBeNull()
  })
})
