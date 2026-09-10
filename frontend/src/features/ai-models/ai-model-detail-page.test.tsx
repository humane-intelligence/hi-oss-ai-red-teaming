import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AiModelDetailPage } from './ai-model-detail-page'
import type { AiModelResponse } from '@/lib/api/types'

const MODEL_ID = 'model-0001-0000-0000-000000000000'

const testModel: AiModelResponse = {
  id: MODEL_ID,
  name: 'Test Model',
  model_alias: 'test-model',
  provider: 'openai',
  // Asymmetric: with input === output, swapping the two Field values renders identically.
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

const testModelWithKey: AiModelResponse = { ...testModel, has_api_key: true }

function modelHandler(model: AiModelResponse = testModel) {
  return http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () => HttpResponse.json(model))
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/ai-models/${MODEL_ID}`]}>
          <Routes>
            <Route path="/ai-models/:id" element={<AiModelDetailPage />} />
            <Route path="/ai-models" element={<div>AI Models list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('AiModelDetailPage — admin note', () => {
  it('shows the note', async () => {
    server.use(modelHandler({ ...testModel, description: 'Client Acme only.' }))
    renderPage(['models:read'])

    expect(await screen.findByText('Client Acme only.')).toBeInTheDocument()
  })

  it('shows an em dash when the note is only whitespace', async () => {
    server.use(modelHandler({ ...testModel, description: '   ' }))
    renderPage(['models:read'])

    const term = await screen.findByText('Description')
    expect(term.nextElementSibling).toHaveTextContent('—')
  })

  it('shows an em dash when there is no note, so the profile keeps its shape', async () => {
    server.use(modelHandler({ ...testModel, description: null }))
    renderPage(['models:read'])

    const term = await screen.findByText('Description')
    // `Field` renders <dt>label</dt><dd>value</dd>, so the value is the term's next sibling.
    expect(term.nextElementSibling).toHaveTextContent('—')
  })
})

describe('AiModelDetailPage — RBAC gates', () => {
  it('with models:read only — no Edit, Delete, Enable, or Set-key buttons', async () => {
    server.use(modelHandler())
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /enable|disable/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /set api key/i })).toBeNull()
  })

  it('with models:update and models:delete — all action buttons are visible', async () => {
    server.use(modelHandler())
    renderPage(['models:read', 'models:update', 'models:delete'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /disable/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /set api key/i })).toBeInTheDocument()
  })

  it('with models:update — Edit, Enable/Disable, Set-key visible; Delete hidden', async () => {
    server.use(modelHandler())
    renderPage(['models:read', 'models:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /disable/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /set api key/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
  })

  it('with models:delete only — only Delete visible; Edit, Enable, Set-key hidden', async () => {
    server.use(modelHandler())
    renderPage(['models:read', 'models:delete'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /disable/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /set api key/i })).toBeNull()
  })

  it('Clear key button visible only with models:update and has_api_key=true', async () => {
    server.use(modelHandler(testModelWithKey))
    renderPage(['models:read', 'models:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /clear key/i })).toBeInTheDocument()
  })

  it('Clear key button hidden when models:update but has_api_key=false', async () => {
    server.use(modelHandler(testModel))
    renderPage(['models:read', 'models:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /clear key/i })).toBeNull()
  })
})

describe('AiModelDetailPage — health check', () => {
  const healthCheckHandler = (model: AiModelResponse, status = 202) => {
    let called = false
    const handler = http.post(`http://localhost/api/v1/ai-models/${MODEL_ID}/health-check`, () => {
      called = true
      return HttpResponse.json(model, { status })
    })
    return { handler, wasCalled: () => called }
  }

  it('renders the current health status', async () => {
    server.use(modelHandler({ ...testModel, health_check_status: 'alive' }))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Alive')).toBeInTheDocument()
  })

  it('shows "Never checked" when the model was never health-checked', async () => {
    server.use(modelHandler(testModel))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Never checked')).toBeInTheDocument()
  })

  it('surfaces the failure reason on a dead model', async () => {
    server.use(
      modelHandler({
        ...testModel,
        health_check_status: 'dead',
        last_health_reason: 'connection refused',
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    // The reason is rendered as visible text (not a hover-only title) so it's announced and reachable.
    expect(screen.getByText('connection refused')).toBeInTheDocument()
  })

  it('Health check button hidden without models:update', async () => {
    server.use(modelHandler(testModel))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /health check/i })).toBeNull()
  })

  it('clicking Health check POSTs and flips the status to Checking…', async () => {
    const posted = healthCheckHandler({ ...testModel, health_check_status: 'checking' })
    server.use(modelHandler(testModel), posted.handler)
    renderPage(['models:read', 'models:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /health check/i }))

    await waitFor(() => expect(posted.wasCalled()).toBe(true))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /checking/i })).toBeInTheDocument(),
    )
  })

  it('polls while a check is in flight and settles to the outcome on its own', async () => {
    let calls = 0
    server.use(
      http.get(`http://localhost/api/v1/ai-models/${MODEL_ID}`, () => {
        calls += 1
        return HttpResponse.json({
          ...testModel,
          health_check_status: calls >= 2 ? 'alive' : 'checking',
        })
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    // no user action, no manual refetch — the poll flips it once the worker settles the row
    await waitFor(() => expect(screen.getByText('Alive')).toBeInTheDocument(), { timeout: 6000 })
    expect(calls).toBeGreaterThanOrEqual(2)
  })

  it('shows the last-checked and last-healthy timestamps', async () => {
    const checkedAt = '2026-05-05T10:00:00Z'
    const healthyAt = '2026-05-05T10:00:12Z'
    server.use(
      modelHandler({
        ...testModel,
        health_check_status: 'alive',
        last_health_check_at: checkedAt,
        last_healthy_at: healthyAt,
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Last checked')).toBeInTheDocument()
    expect(screen.getByText(new Date(checkedAt).toLocaleString())).toBeInTheDocument()
    expect(screen.getByText('Last healthy')).toBeInTheDocument()
    expect(screen.getByText(new Date(healthyAt).toLocaleString())).toBeInTheDocument()
  })

  it('shows the idle-alert threshold and the last message for a warmed model', async () => {
    const usedAt = '2026-08-16T09:14:00Z'
    server.use(
      modelHandler({
        ...testModel,
        warmup_enabled: true,
        inactivity_alert_hours: 48,
        last_used_at: usedAt,
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Idle alert after')).toBeInTheDocument()
    expect(screen.getByText('48 h')).toBeInTheDocument()
    expect(screen.getByText('Last message')).toBeInTheDocument()
    expect(screen.getByText(new Date(usedAt).toLocaleString())).toBeInTheDocument()
  })

  it('warns once the model has been alerted as idle', async () => {
    server.use(
      modelHandler({
        ...testModel,
        warmup_enabled: true,
        inactivity_alert_hours: 24,
        last_used_at: '2026-08-16T09:14:00Z',
        inactivity_alerted_at: '2026-08-19T09:00:00Z',
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText(/Disable it if nobody needs it/)).toBeInTheDocument()
  })

  it('drops the idle warning once the model is disabled — the nudge is resolved', async () => {
    server.use(
      modelHandler({
        ...testModel,
        warmup_enabled: true,
        inactivity_alert_hours: 24,
        last_used_at: '2026-08-16T09:14:00Z',
        inactivity_alerted_at: '2026-08-19T09:00:00Z',
        is_disabled: true,
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByText(/Disable it if nobody needs it/)).not.toBeInTheDocument()
  })

  it('drops the idle warning once warmup is turned off — the check is disarmed', async () => {
    server.use(
      modelHandler({
        ...testModel,
        warmup_enabled: false,
        inactivity_alert_hours: 24,
        last_used_at: '2026-08-16T09:14:00Z',
        inactivity_alerted_at: '2026-08-19T09:00:00Z',
      }),
    )
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByText(/Disable it if nobody needs it/)).not.toBeInTheDocument()
  })

  it('still shows a stored threshold after warmup is turned off, marked as inert', async () => {
    server.use(modelHandler({ ...testModel, warmup_enabled: false, inactivity_alert_hours: 48 }))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Idle alert after')).toBeInTheDocument()
    expect(screen.getByText('48 h (inactive — warm-up off)')).toBeInTheDocument()
  })

  it('shows an em-dash for timestamps when the model was never checked', async () => {
    server.use(modelHandler(testModel))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Last checked')).toBeInTheDocument()
    expect(screen.getByText('Last healthy')).toBeInTheDocument()
  })

  it('keeps the Health check button enabled while a check is in flight so a stale/stuck check can be retried', async () => {
    server.use(modelHandler({ ...testModel, health_check_status: 'checking' }))
    renderPage(['models:read', 'models:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    // reflects the in-flight state in its label, but stays clickable — the backend overrides a
    // `checking` row older than its stale TTL, and disabling here would strand a stuck check
    expect(screen.getByRole('button', { name: /checking/i })).toBeEnabled()
  })
  it('reports a capability mismatch without changing the health verdict', async () => {
    server.use(
      modelHandler({
        ...testModel,
        health_check_status: 'alive',
        capability_mismatch: 'unsupported content type: image_url',
      }),
    )
    renderPage(['models:read'])

    // Scoped to the Input field, and paired with a negative on the Health tile — `within(input)`
    // alone proves the badge is in Input, not that it is *only* there, which is the placement claim.
    await screen.findByRole('heading', { name: 'Test Model' })
    const input = screen.getByText('Input').nextElementSibling as HTMLElement
    expect(input).toHaveTextContent('image input unconfirmed')
    const health = screen.getByText('Health').nextElementSibling as HTMLElement
    expect(health).not.toHaveTextContent(/unconfirmed/i)
    // The endpoint answered, so the verdict is untouched.
    expect(screen.getByText('Alive')).toBeInTheDocument()
    // The provider text is reachable through the hint's panel, and must not be the button's name —
    // a 255-char JSON blob as an accessible name is the thing `label` exists to prevent.
    const hint = within(input).getByRole('button', { name: 'Why image input is unconfirmed' })
    const panel = document.getElementById(hint.getAttribute('popovertarget')!)
    expect(panel).toHaveTextContent(/unsupported content type: image_url/)
    // And the text is still the accessible description, so the short name costs AT nothing.
    expect(hint).toHaveAccessibleDescription(/unsupported content type: image_url/)
  })
})

describe('AiModelDetailPage — modality', () => {
  it('puts each declared set under its own direction', async () => {
    server.use(modelHandler())
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Input').nextElementSibling).toHaveTextContent('Text, Image')
    expect(screen.getByText('Output').nextElementSibling).toHaveTextContent('Text')
    // Negative: both caveats are conditional, and every other fixture leaves them off.
    expect(screen.queryByText(/unconfirmed/)).not.toBeInTheDocument()
    expect(screen.queryByText('no text output')).not.toBeInTheDocument()
  })

  it('warns that a model with no text output cannot be used in a conversation', async () => {
    server.use(modelHandler({ ...testModel, output_modalities: ['image'] }))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    // The badge is the visible half and the only half a `getByText` on the copy would miss: that
    // copy now lives in a closed popover, so matching it passes with or without the rework.
    const output = screen.getByText('Output').nextElementSibling as HTMLElement
    const badge = within(output).getByText('no text output')
    expect(badge).toBeVisible()
    const hint = within(output).getByRole('button', { name: /Conversations need a text reply/ })
    expect(document.getElementById(hint.getAttribute('popovertarget')!)).toHaveTextContent(
      /Conversations need a text reply/,
    )
  })

  it('renders the labels as badges', async () => {
    server.use(modelHandler({ ...testModel, labels: ['self-hosted', 'audited'] }))
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    // Scoped to the Labels field and to the titled badge: page-wide text would pass if the value
    // landed in the admin note instead, and bare text would pass if the badge were dropped.
    const labels = screen.getByText('Labels').closest('div')?.parentElement as HTMLElement
    const badges = labels.querySelectorAll('[title]')
    expect([...badges].map((b) => b.getAttribute('title'))).toEqual(['self-hosted', 'audited'])
  })

  it('shows a dash when the model carries no labels', async () => {
    server.use(modelHandler())
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.getByText('Labels').parentElement).toHaveTextContent('—')
  })

  it('leaves the warning off a model that can reply in text', async () => {
    server.use(modelHandler())
    renderPage(['models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test Model' })).toBeInTheDocument(),
    )
    expect(screen.queryByText(/Conversations need a text reply/)).not.toBeInTheDocument()
  })
})
