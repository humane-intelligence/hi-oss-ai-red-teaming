import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { queryClient } from '@/lib/query'
import { AiModelFormPage } from './ai-model-form-page'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const MODEL_ID = 'model-001-0000-0000-000000000000'

function modelResponse(
  parameters: Record<string, unknown>,
  overrides: Record<string, unknown> = {},
) {
  return {
    id: MODEL_ID,
    name: 'Claude',
    model_alias: 'claude',
    provider: 'openai',
    input_modalities: ['text'],
    output_modalities: ['text'],
    provider_model_id: 'claude-x',
    endpoint_name: null,
    inference_endpoint: null,
    icon_file: null,
    labels: [],
    is_disabled: false,
    warmup_enabled: false,
    has_api_key: false,
    parameters,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderForm(path: string, client?: QueryClient) {
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/ai-models" element={<div>list</div>} />
          <Route path="/ai-models/new" element={<AiModelFormPage />} />
          <Route path="/ai-models/:id/edit" element={<AiModelFormPage />} />
          <Route path="/ai-models/:id" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AiModelFormPage', () => {
  it('sends parameters on create when advanced fields are filled', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.click(screen.getByRole('button', { name: /advanced model parameters/i }))
    await user.type(screen.getByLabelText('Temperature'), '0.7')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.parameters).toEqual({ temperature: 0.7 })
  })

  it('sends the description, trimmed, on the managed-provider create path', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    // `kind=provider` hides the endpoint fields, so this also pins the note to the shared block.
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.type(
      screen.getByLabelText('Description'),
      '  Client Acme only.{enter}Second line.  ',
    )
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    // The newline survives: a `Textarea`, not an `Input` — the note is prose, and the profile
    // renders it with `whitespace-pre-line`.
    expect(captured!.description).toBe('Client Acme only.\nSecond line.')
  })

  it('enables the idle-alert threshold only once warmup is checked, and sends it', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    expect(screen.getByLabelText('Idle alert after (hours)')).toBeDisabled()
    await user.click(screen.getByLabelText('Warm up before chatting'))
    await user.type(screen.getByLabelText('Idle alert after (hours)'), '48')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.inactivity_alert_hours).toBe(48)
  })

  it('hides the parameter panel and sends the flag once advanced params are disabled', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    const panelToggle = { name: /Advanced model parameters/ } as const
    expect(screen.getByRole('button', panelToggle)).toBeInTheDocument()

    await user.click(screen.getByLabelText('Disable advanced parameters'))
    expect(screen.queryByRole('button', panelToggle)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.advanced_params_disabled).toBe(true)
  })

  it('sends a null idle-alert threshold when the field is left blank', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.click(screen.getByLabelText('Warm up before chatting'))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.inactivity_alert_hours).toBeNull()
  })

  it('sends an explicit null for the description when it was never filled', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.description).toBeNull()
  })

  it('sends an explicit null when the description holds only whitespace', async () => {
    // The never-filled and cleared-on-edit cases both reach `.trim() || null` from an empty string;
    // this is the one that reaches it from a non-empty one.
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.type(screen.getByLabelText('Description'), '   ')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.description).toBeNull()
  })

  it('prefills the description when editing', async () => {
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { description: 'Existing note.' })),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit`)

    await waitFor(() => expect(screen.getByLabelText('Description')).toHaveValue('Existing note.'))
  })

  it('clears the description when the field is emptied', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { description: 'Existing note.' })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const field = await screen.findByLabelText('Description')
    await waitFor(() => expect(field).toHaveValue('Existing note.'))
    await user.clear(field)
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.description).toBeNull()
  })

  it('refuses a 21st label inline, before any request', async () => {
    // The contract publishes maxItems: 20, so the form can say no without a round-trip — and it says
    // it on the add, not a click later on Create.
    let posted = false
    server.use(
      http.get('http://localhost/api/v1/ai-models/labels', () =>
        HttpResponse.json(Array.from({ length: 21 }, (_, i) => `label-${i}`)),
      ),
      http.post('http://localhost/api/v1/ai-models', () => {
        posted = true
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    const labels = await screen.findByLabelText('Labels (optional)')
    for (let i = 0; i < 21; i++) {
      await user.click(labels)
      await user.click(await screen.findByRole('option', { name: `label-${i}` }))
    }

    expect(await screen.findByText('At most 20 labels')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(posted).toBe(false)
  })

  it('counts a label length in code points, so an astral label is not double-charged', async () => {
    // maxLength: 64 counts code points on the API. `.length` would make 64 emoji 128 units and the
    // form would refuse a label the server accepts.
    const sixtyFourEmoji = '\u{1F512}'.repeat(64)
    expect(sixtyFourEmoji.length).toBe(128)
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get('http://localhost/api/v1/ai-models/labels', () => HttpResponse.json([])),
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    const labels = await screen.findByLabelText('Labels (optional)')
    await user.type(labels, sixtyFourEmoji)
    await user.click(await screen.findByRole('option', { name: /create/i }))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.labels).toEqual([sixtyFourEmoji])
  })

  it('states both label caps in the hint, so neither is learned by tripping it', async () => {
    server.use(http.get('http://localhost/api/v1/ai-models/labels', () => HttpResponse.json([])))
    renderForm('/ai-models/new?kind=provider')

    const labels = await screen.findByLabelText('Labels (optional)')
    const hintId = labels.getAttribute('aria-describedby')
    expect(document.getElementById(hintId!)).toHaveTextContent(
      'Up to 20 labels, 64 characters each',
    )
  })

  it('puts the per-label length error on the field, rather than leaving Create dead', async () => {
    server.use(
      http.get('http://localhost/api/v1/ai-models/labels', () => HttpResponse.json([])),
      http.post('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json({ id: MODEL_ID }, { status: 201 }),
      ),
    )
    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    const labels = await screen.findByLabelText('Labels (optional)')
    await user.type(labels, 'x'.repeat(65))
    await user.click(await screen.findByRole('option', { name: /create/i }))

    expect(await screen.findByText('At most 64 characters per label')).toBeInTheDocument()
    // Still on the form, error still up: a refused submit is only observable as *not* navigating,
    // and a POST that never fires cannot be awaited.
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('At most 64 characters per label')).toBeInTheDocument()
    expect(screen.queryByText('list')).toBeNull()
  })

  it('sends the labels the operator picked and typed on create', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get('http://localhost/api/v1/ai-models/labels', () =>
        HttpResponse.json(['self-hosted', 'audited']),
      ),
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')

    const labels = screen.getByLabelText('Labels (optional)')
    await user.click(labels)
    // One from the vocabulary …
    await user.click(await screen.findByRole('option', { name: 'self-hosted' }))
    // … and one that does not exist yet.
    await user.type(labels, 'fine-tuning needed')
    await user.click(await screen.findByRole('option', { name: /create/i }))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.labels).toEqual(['self-hosted', 'fine-tuning needed'])
  })

  it('prefills the label chips from the stored model on edit', async () => {
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { labels: ['audited', 'self-hosted'] })),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit`)

    expect(await screen.findByRole('button', { name: /remove audited/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /remove self-hosted/i })).toBeInTheDocument()
  })

  it('preserves stored keys the editor does not manage on edit', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({ temperature: 0.5, stop_sequences: ['</end>'] })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({ temperature: 0.9, stop_sequences: ['</end>'] }))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    // Params auto-expand because the model already has overrides.
    const temperature = await screen.findByLabelText('Temperature')
    await waitFor(() => expect(temperature).toHaveValue(0.5))
    await user.clear(temperature)
    await user.type(temperature, '0.9')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.parameters).toEqual({ stop_sequences: ['</end>'], temperature: 0.9 })
  })

  it('sends the managed-provider kind to the create route without endpoint fields', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    expect(screen.queryByLabelText(/inference endpoint/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/endpoint name/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Generic' })).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.provider).toBe('openai')
    expect(captured!.inference_endpoint).toBeNull()
    expect(captured!.endpoint_name).toBeNull()
  })

  it('requires an inference endpoint on the custom kind', async () => {
    let called = false
    server.use(
      http.post('http://localhost/api/v1/ai-models', () => {
        called = true
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=custom')

    await user.type(screen.getByLabelText('Name'), 'Local')
    await user.type(screen.getByLabelText('Alias'), 'local')
    await user.type(screen.getByLabelText('Provider model id'), 'qwen')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(
      await screen.findByText(/generic provider has no vendor-hosted URL/i),
    ).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('refuses an inference endpoint that carries no http(s) scheme', async () => {
    let called = false
    server.use(
      http.post('http://localhost/api/v1/ai-models', () => {
        called = true
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=custom')

    await user.type(screen.getByLabelText('Name'), 'Local')
    await user.type(screen.getByLabelText('Alias'), 'local')
    await user.type(screen.getByLabelText('Provider model id'), 'qwen')
    await user.type(screen.getByLabelText('Inference endpoint'), 'slm:8080/v1')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText(/must start with http/i)).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('drops the stale required-endpoint error when the provider stops requiring one', async () => {
    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=custom')

    await user.type(screen.getByLabelText('Name'), 'Local')
    await user.type(screen.getByLabelText('Alias'), 'local')
    await user.type(screen.getByLabelText('Provider model id'), 'qwen')
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(
      await screen.findByText(/generic provider has no vendor-hosted URL/i),
    ).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Provider'), 'azure')

    expect(screen.queryByText(/generic provider has no vendor-hosted URL/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Inference endpoint (optional)')).toBeInTheDocument()
  })

  it('sends the custom endpoint when the custom kind supplies one', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ id: MODEL_ID }, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=custom')

    await user.type(screen.getByLabelText('Name'), 'Local')
    await user.type(screen.getByLabelText('Alias'), 'local')
    await user.type(screen.getByLabelText('Provider model id'), 'qwen')
    await user.type(screen.getByLabelText('Inference endpoint'), 'http://slm:8080/v1')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.provider).toBe('generic')
    expect(captured!.inference_endpoint).toBe('http://slm:8080/v1')
  })

  it('re-picks the kind in place rather than sending the user back to the list', async () => {
    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=custom')

    await user.type(screen.getByLabelText('Inference endpoint'), 'http://slm:8080/v1')
    await user.click(screen.getByRole('button', { name: /change/i }))
    await user.click(screen.getByRole('button', { name: /Provider API/ }))

    expect(screen.getByLabelText('Name')).toBeInTheDocument()
    expect(screen.queryByText('list')).not.toBeInTheDocument()
    // The kind swapped, and the URL it collected went with it.
    expect(screen.getByText('Provider API')).toBeInTheDocument()
    expect(screen.queryByLabelText(/inference endpoint/i)).not.toBeInTheDocument()
    // `setValue` writes to the uncontrolled <select> before React swaps its options, so
    // pin that the DOM value and the form state agree on the new kind's first provider.
    expect(screen.getByLabelText('Provider')).toHaveValue('openai')
  })

  it('bounces a create without a kind back to the chooser', async () => {
    renderForm('/ai-models/new')

    // No kind means the dialog was bypassed — the choice is never guessed.
    expect(await screen.findByText('list')).toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).not.toBeInTheDocument()
  })

  it('edits with the full provider list and the endpoint fields visible', async () => {
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(
          modelResponse({}, { provider: 'generic', inference_endpoint: 'http://slm:8080/v1' }),
        ),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const endpoint = await screen.findByLabelText('Inference endpoint')
    expect(endpoint).toHaveValue('http://slm:8080/v1')
    // The kind is a create-time choice; an existing row is edited against the whole
    // enum, with nothing that can rewrite `provider` behind the user's back.
    expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Generic' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /change/i })).not.toBeInTheDocument()
  })

  it('ignores a hand-typed kind on the edit route', async () => {
    // Honouring it would narrow the provider list past the row's own value and the
    // select would silently fall back to the first option it does offer.
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(
          modelResponse({}, { provider: 'generic', inference_endpoint: 'http://slm:8080/v1' }),
        ),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit?kind=provider`)

    await waitFor(() => expect(screen.getByLabelText('Provider')).toHaveValue('generic'))
    expect(screen.getByRole('option', { name: 'Generic' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /change/i })).not.toBeInTheDocument()
  })

  it('preserves the fields it no longer renders across an unrelated edit', async () => {
    // A vendor row may point at a compatible proxy — dispatch feeds `api_base` for
    // every provider — and endpoint_name/icon_file have no input at all, so an edit
    // that never touches them must not null them out.
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(
          modelResponse(
            {},
            {
              provider: 'openai',
              inference_endpoint: 'https://proxy.internal/v1',
              endpoint_name: 'prod-proxy',
              icon_file: 'openai.svg',
            },
          ),
        ),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const name = await screen.findByLabelText('Name')
    await waitFor(() => expect(name).toHaveValue('Claude'))
    await user.clear(name)
    await user.type(name, 'Renamed')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    // The URL is preserved by staying out of the patch entirely — omitted means unchanged.
    expect(captured).not.toHaveProperty('inference_endpoint')
    expect(captured).not.toHaveProperty('provider')
    expect(captured!.endpoint_name).toBe('prod-proxy')
    expect(captured!.icon_file).toBe('openai.svg')
  })

  it('lets a legacy generic row without a URL still be saved', async () => {
    // The backend only enforces the URL when the patch touches provider/endpoint, so a
    // rename must leave that pair out of the body — otherwise such a row, which predates
    // the rule, could never be renamed. The handler enforces the real rule, so this test
    // fails if the form starts sending the pair unconditionally.
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { provider: 'generic', inference_endpoint: null })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        // The backend rule, on the merged row: it fires only when the patch touches the
        // pair, and the stored row here is `generic` with no URL.
        const touchesPair = 'provider' in body || 'inference_endpoint' in body
        const provider = 'provider' in body ? body.provider : 'generic'
        const url = 'inference_endpoint' in body ? body.inference_endpoint : null
        if (touchesPair && provider === 'generic' && !url) {
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: 'Bad Request',
              status: 400,
              detail:
                'A generic provider has no vendor-hosted base URL, so inference_endpoint is required.',
              errors: [
                {
                  loc: ['body', 'inference_endpoint'],
                  msg: 'Required',
                  type: 'inference_endpoint_required',
                },
              ],
            },
            { status: 400, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        captured = body
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const name = await screen.findByLabelText('Name')
    // The field exists before the row lands; clearing early races `reset()`.
    await waitFor(() => expect(name).toHaveValue('Claude'))
    await user.clear(name)
    await user.type(name, 'Legacy renamed')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // The save landed (the handler would have 400'd on the pair) and navigated on.
    expect(await screen.findByText('detail')).toBeInTheDocument()
    expect(captured!.name).toBe('Legacy renamed')
  })

  it('sends the provider/endpoint pair once it actually moves', async () => {
    // The mirror of the case above: the rule must still reach a row the user is
    // changing, so the pair rides along as soon as either half is edited.
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { provider: 'generic', inference_endpoint: null })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const url = await screen.findByLabelText(/inference endpoint/i)
    await waitFor(() => expect(screen.getByLabelText('Name')).toHaveValue('Claude'))
    await user.type(url, 'http://slm:8080/v1')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.inference_endpoint).toBe('http://slm:8080/v1')
    expect(captured).not.toHaveProperty('provider')
  })

  it('saves a model with empty parameters (Advanced section collapsed)', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({})),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    // Empty params → Advanced section stays collapsed; the numeric knobs must still
    // reset to '' (not undefined), or the z.string() schema silently blocks Save.
    const save = await screen.findByRole('button', { name: 'Save' })
    expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText("Disabled (won't be used for new conversations)"))
    await user.click(save)

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.is_disabled).toBe(true)
  })

  it('keeps text input on a create the user never touched, and adds image when picked', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/ai-models', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}), { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'Vision')
    await user.type(screen.getByLabelText('Alias'), 'vision')
    await user.type(screen.getByLabelText('Provider model id'), 'gpt-4o')
    // Rendered disabled, so the browser never submits it — the payload must carry it anyway.
    const textInput = screen.getByLabelText('Text input') as HTMLInputElement
    expect(textInput.disabled).toBe(true)
    expect(textInput.checked).toBe(true)
    await user.click(screen.getByLabelText('Image input'))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.input_modalities).toEqual(['text', 'image'])
    expect(captured!.output_modalities).toEqual(['text'])
  })

  it('blocks a submit that clears every output modality', async () => {
    let called = false
    server.use(
      http.post('http://localhost/api/v1/ai-models', async () => {
        called = true
        return HttpResponse.json(modelResponse({}), { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider')

    await user.type(screen.getByLabelText('Name'), 'No output')
    await user.type(screen.getByLabelText('Alias'), 'no-output')
    await user.type(screen.getByLabelText('Provider model id'), 'gpt-4o')
    await user.click(screen.getByLabelText('Text output'))
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('Select at least one output modality')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it("starts an edit with the row's own modalities checked", async () => {
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { input_modalities: ['text', 'image'] })),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit`)

    await screen.findByDisplayValue('claude')
    expect((screen.getByLabelText('Image input') as HTMLInputElement).checked).toBe(true)
  })

  it('drops image input when the edit unchecks it', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { input_modalities: ['text', 'image'] })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const image = (await screen.findByLabelText('Image input')) as HTMLInputElement
    await waitFor(() => expect(image.checked).toBe(true))
    await user.click(image)
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // `text` is mandatory and never leaves the payload, but `image` must actually go.
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.input_modalities).toEqual(['text'])
  })

  it("starts an edit with the row's own output modalities checked", async () => {
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { output_modalities: ['text', 'image'] })),
      ),
    )

    renderForm(`/ai-models/${MODEL_ID}/edit`)

    await screen.findByDisplayValue('claude')
    expect((screen.getByLabelText('Image output') as HTMLInputElement).checked).toBe(true)
  })

  it('keeps an untouched output set on a save that changes only the name', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { output_modalities: ['text', 'image'] })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const image = (await screen.findByLabelText('Image output')) as HTMLInputElement
    await waitFor(() => expect(image.checked).toBe(true))
    await user.type(screen.getByLabelText('Name'), ' v2')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // A PATCH replaces the set wholesale, so a load that dropped `image` would silently
    // narrow the row on a save that never touched the fieldset.
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.output_modalities).toEqual(['text', 'image'])
  })

  it('drops image output when the edit unchecks it', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { output_modalities: ['text', 'image'] })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    const image = (await screen.findByLabelText('Image output')) as HTMLInputElement
    await waitFor(() => expect(image.checked).toBe(true))
    await user.click(image)
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.output_modalities).toEqual(['text'])
  })

  it('reports a duplicate-name conflict inline, with no toast beside it', async () => {
    // The app's own client, not a bare one: the toast suppression for a `Problem`
    // carrying `errors[]` lives in its MutationCache (`@/lib/query`), so a bare
    // QueryClient would pass this test even if that coupling broke.
    server.use(
      http.post('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'A model with this name already exists.',
            errors: [
              {
                loc: ['body', 'name'],
                msg: 'A model with this name already exists.',
                type: 'duplicate_name',
              },
            ],
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const user = userEvent.setup()
    renderForm('/ai-models/new?kind=provider', queryClient)

    await user.type(screen.getByLabelText('Name'), 'Claude')
    await user.type(screen.getByLabelText('Alias'), 'claude')
    await user.type(screen.getByLabelText('Provider model id'), 'claude-x')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('A model with this name already exists.')).toBeInTheDocument()
    expect(toast.error).not.toHaveBeenCalled()
    queryClient.clear()
  })

  it('keeps a stored threshold when an edit unchecks warmup', async () => {
    // Regression: the submit used to couple the value to the checkbox, so an unrelated
    // edit silently cleared the column — invisible in the UI and unrecoverable.
    let captured: Record<string, unknown> | null = null
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () =>
        HttpResponse.json(modelResponse({}, { warmup_enabled: true, inactivity_alert_hours: 48 })),
      ),
      http.patch(`http://localhost/api/v1/ai-models/${MODEL_ID}`, async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(modelResponse({}))
      }),
    )

    const user = userEvent.setup()
    renderForm(`/ai-models/${MODEL_ID}/edit`)

    await waitFor(() => expect(screen.getByLabelText('Idle alert after (hours)')).toHaveValue('48'))
    await user.click(screen.getByLabelText('Warm up before chatting'))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.inactivity_alert_hours).toBe(48)
    expect(captured!.warmup_enabled).toBe(false)
  })

  it.each(['0', '8761'])(
    'blocks save with a visible error when the threshold is %s (outside 1-8760)',
    async (value) => {
      const user = userEvent.setup()
      renderForm('/ai-models/new?kind=provider')

      await user.click(screen.getByLabelText('Warm up before chatting'))
      await user.type(screen.getByLabelText('Idle alert after (hours)'), value)
      await user.click(screen.getByRole('button', { name: 'Create' }))

      expect(
        await screen.findByText('Must be a whole number of hours between 1 and 8760'),
      ).toBeVisible()
    },
  )
})
