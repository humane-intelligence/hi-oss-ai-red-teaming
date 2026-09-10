import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { GroupCreatePage } from './group-create-page'
import type { EvaluationResponse } from '@/lib/api/types'
import { licenseStub } from '@/features/licenses/test-fixtures'
import { parentGroupHandler } from './test-fixtures'

const EVAL_ID = 'eval-cmp1-0000-0000-000000000000'
const MODEL_A = 'model-aaa0-0000-0000-000000000000'
const MODEL_B = 'model-bbb0-0000-0000-000000000000'
const GROUP_ID = 'grp-cmp1-0000-0000-000000000000'

const evaluation: EvaluationResponse = {
  id: EVAL_ID,
  evaluation_group_id: 'grp-0000-0000-0000-000000000000',
  created_by_id: 'user-000-0000-0000-000000000000',
  title: 'Compare eval',
  description: 'desc',
  status: 'approved',
  mask_models_enabled: false,
  tags_enabled: true,
  tags_restricted: false,
  effective_license: licenseStub('CC-BY-4.0'),
  models: [
    {
      assignment_id: MODEL_A,
      name: 'gpt-4o',
      warmup_enabled: false,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
    {
      assignment_id: MODEL_B,
      name: 'claude-3-opus',
      warmup_enabled: false,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
  ],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const SCENARIO_ID = 'scn-0001-0000-0000-000000000000'

function groupResponse() {
  return {
    id: GROUP_ID,
    user_id: 'user-000-0000-0000-000000000000',
    evaluation_id: EVAL_ID,
    name: 'Compare',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    conversations: [],
  }
}

function baseHandlers() {
  return [
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => HttpResponse.json(evaluation)),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
      HttpResponse.json({
        items: [
          {
            id: SCENARIO_ID,
            name: 'Prompt injection',
            description: '',
            evaluation_id: EVAL_ID,
            position: 0,
          },
        ],
        total: 1,
        limit: 100,
        offset: 0,
      }),
    ),
    parentGroupHandler(evaluation.evaluation_group_id),
  ]
}

function renderPage(
  route = `/evaluations/${EVAL_ID}/conversation-groups/new`,
  permissions = ['conversations:create', 'conversations:participate'],
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/evaluations/:id/conversation-groups/new" element={<GroupCreatePage />} />
            <Route
              path="/evaluations/:id/conversation-groups/:groupId"
              element={<div>GROUP PAGE</div>}
            />
            <Route path="/evaluations/:id" element={<div>Evaluation detail</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

async function selectScenario(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByLabelText('Scenario'))
  await user.click(await screen.findByRole('option', { name: 'Prompt injection' }))
}

describe('GroupCreatePage', () => {
  it('shows one model row by default', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    expect(screen.queryByLabelText('Model 2')).toBeNull()
  })

  it('adds and removes model rows', async () => {
    const user = userEvent.setup()
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add model/i }))
    expect(screen.getByLabelText('Model 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /remove model 2/i }))
    expect(screen.queryByLabelText('Model 2')).toBeNull()
  })

  it('creates one entry per row with the chosen models', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(groupResponse(), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /add model/i }))
    await user.click(screen.getByLabelText('Model 2'))
    await user.click(await screen.findByRole('option', { name: 'claude-3-opus' }))
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { models: Array<{ evaluation_ai_model_id: string }> }
    expect(body.models.map((m) => m.evaluation_ai_model_id)).toEqual([MODEL_A, MODEL_B])
  })

  it('includes per-row parameters when set', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(groupResponse(), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /add model/i }))
    // Open row 1 params and set a temperature.
    await user.click(screen.getAllByRole('button', { name: /advanced model parameters/i })[0]!)
    await user.type(await screen.findByLabelText('Temperature'), '0.5')
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { models: Array<{ parameters?: { temperature?: number } }> }
    expect(body.models[0]?.parameters?.temperature).toBe(0.5)
    expect(Object.prototype.hasOwnProperty.call(body.models[1], 'parameters')).toBe(false)
  })

  it('offers no params for a flagged model, and sends none even if typed first', async () => {
    // The row keeps its typed values after the panel goes away, so the guard has to be on
    // submit as well as on render — otherwise an override lands that is never applied.
    let captured: unknown = null
    const flagged = {
      ...evaluation,
      models: [
        evaluation.models![0]!,
        { ...evaluation.models![1]!, advanced_params_disabled: true },
      ],
    }
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => HttpResponse.json(flagged)),
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(groupResponse(), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    // The single row defaults to the non-flagged model, so the panel is on offer.
    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /advanced model parameters/i }))
    await user.type(await screen.findByLabelText('Temperature'), '0.5')

    // Point the same row at the flagged model: the toggle and panel go away.
    await user.click(screen.getByLabelText('Model 1'))
    await user.click(await screen.findByRole('option', { name: 'claude-3-opus' }))
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /advanced model parameters/i }),
      ).not.toBeInTheDocument(),
    )
    expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument()

    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { models: Array<Record<string, unknown>> }
    expect(Object.prototype.hasOwnProperty.call(body.models[0], 'parameters')).toBe(false)
  })

  it('includes a per-row title only when set', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(groupResponse(), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /add model/i }))
    await user.type(screen.getByLabelText('Title 1 (optional)'), 'Direct ask')
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { models: Array<{ title?: string }> }
    expect(body.models[0]?.title).toBe('Direct ask')
    expect(Object.prototype.hasOwnProperty.call(body.models[1], 'title')).toBe(false)
  })

  it('shows an inline error when creating the group fails validation', async () => {
    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`, () =>
        HttpResponse.json(
          {
            detail: 'Request body failed validation.',
            errors: [
              {
                loc: ['body', 'models', 0, 'title'],
                msg: 'Title must not be blank.',
                type: 'value_error',
              },
            ],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    expect(await screen.findByText('Title must not be blank.')).toBeInTheDocument()
  })

  it('navigates to the persistent group page once created', async () => {
    const user = userEvent.setup()
    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`, () =>
        HttpResponse.json(groupResponse(), { status: 201 }),
      ),
    )
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await selectScenario(user)
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(screen.getByText('GROUP PAGE')).toBeInTheDocument())
  })

  it('keeps Start disabled until a scenario is chosen', async () => {
    const user = userEvent.setup()
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /start conversation/i })).toBeDisabled()

    await selectScenario(user)
    expect(screen.getByRole('button', { name: /start conversation/i })).toBeEnabled()
  })

  it('explains and blocks when the evaluation has no scenarios', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json(evaluation),
      ),
      parentGroupHandler(evaluation.evaluation_group_id),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    expect(
      screen.getByText(
        'Every conversation targets a scenario — add one on the evaluation page first.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start conversation/i })).toBeDisabled()
  })

  it('preselects the scenario from the ?scenario= query param', async () => {
    let captured: string | null = null
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json(evaluation),
      ),
      parentGroupHandler(evaluation.evaluation_group_id),
      // Two scenarios, and the deep-linked one is *not* first: picking it cannot be
      // confused with falling back to the head of the list.
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({
          items: [
            {
              id: 'scn-0002-0000-0000-000000000000',
              name: 'Data exfiltration',
              description: '',
              evaluation_id: EVAL_ID,
              position: 0,
            },
            {
              id: SCENARIO_ID,
              name: 'Prompt injection',
              description: '',
              evaluation_id: EVAL_ID,
              position: 1,
            },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      // Registered per-scenario on purpose: the preselection is now observable in the
      // URL, not the body, so a POST to the wrong scenario would not match at all.
      http.post(
        `http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`,
        ({ request }) => {
          captured = request.url
          return HttpResponse.json(groupResponse(), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage(`/evaluations/${EVAL_ID}/conversation-groups/new?scenario=${SCENARIO_ID}`)

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /start conversation/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toBe(`http://localhost/api/v1/scenarios/${SCENARIO_ID}/conversation-groups`)
  })

  it('drops a stale ?scenario= deep-link so the picker shows nothing chosen', async () => {
    // A native select whose controlled value matches no option falls back to displaying
    // the first enabled option — the picker would look chosen while Start stays dead.
    const user = userEvent.setup()
    server.use(...baseHandlers())
    renderPage(
      `/evaluations/${EVAL_ID}/conversation-groups/new?scenario=scn-gone-0000-0000-000000000000`,
    )

    await waitFor(() => expect(screen.getByLabelText('Model 1')).toBeInTheDocument())
    // The trigger falls back to its placeholder when nothing is chosen.
    await waitFor(() =>
      expect(screen.getByLabelText('Scenario')).toHaveTextContent('Select a scenario…'),
    )
    expect(screen.getByRole('button', { name: /start conversation/i })).toBeDisabled()

    await selectScenario(user)
    expect(screen.getByRole('button', { name: /start conversation/i })).toBeEnabled()
  })
})

describe('GroupCreatePage — in-group authority', () => {
  // The parent group carries object-scope authority; the route gate is coarse, so this page is
  // where `conversations:create` is actually enforced.
  it('lets a group-scoped red teamer start a conversation without the global permission', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:create',
      ]),
      ...baseHandlers(),
    )
    renderPage(undefined, ['evaluations:read'])

    const user = userEvent.setup()
    await selectScenario(user)
    const start = await screen.findByRole('button', { name: 'Start conversation' })
    expect(start).toBeEnabled()
    expect(screen.queryByText(/do not have permission/i)).toBeNull()
  })

  it('refuses when neither the JWT nor the in-group role grants create', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, ['conversations:read']),
      ...baseHandlers(),
    )
    renderPage(undefined, ['evaluations:read'])

    expect(await screen.findByText(/access to this section/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start conversation' })).toBeNull()
  })
})
