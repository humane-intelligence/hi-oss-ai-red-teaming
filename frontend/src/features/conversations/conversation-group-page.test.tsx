import { beforeEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { AUTH_USER_ID, authWrapper } from '@/lib/auth/auth.testutils'
import { parentGroupHandler } from './test-fixtures'
import { ConversationGroupPage } from './conversation-group-page'
import type { ConversationResponse, EvaluationResponse } from '@/lib/api/types'
import { licenseStub } from '@/features/licenses/test-fixtures'

const EVAL_ID = 'eval-grp1-0000-0000-000000000000'
const MODEL_A = 'model-aaa0-0000-0000-000000000000'
const MODEL_B = 'model-bbb0-0000-0000-000000000000'
const CONV_A = 'conv-aaa0-0000-0000-000000000000'
const CONV_B = 'conv-bbb0-0000-0000-000000000000'
const GROUP_ID = 'grp-side1-0000-0000-000000000000'
const SCEN_ID = 'scen-0001-0000-0000-000000000000'
const TASK_ID = 'task-0001-0000-0000-000000000000'

// Counted, not just answered: a pane the caller may not write to must not probe warmup.
let warmupCalls = 0
// Likewise for completions — a caller who may not read them must not ask N times.
let completedTasksCalls = 0
beforeEach(() => {
  warmupCalls = 0
  completedTasksCalls = 0
})

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
      warmup_enabled: true,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
    {
      assignment_id: MODEL_B,
      name: 'claude-3-opus',
      warmup_enabled: true,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
  ],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function conv(id: string, modelId: string): ConversationResponse {
  return {
    id,
    user_id: AUTH_USER_ID,
    evaluation_id: EVAL_ID,
    evaluation_ai_model_id: modelId,
    content_protected: false,
    scenario_id: SCEN_ID,
    conversation_group_id: GROUP_ID,
    effective_license: licenseStub('CC-BY-4.0'),
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

function group(conversations = [conv(CONV_A, MODEL_A), conv(CONV_B, MODEL_B)]) {
  return {
    id: GROUP_ID,
    user_id: AUTH_USER_ID,
    evaluation_id: EVAL_ID,
    scenario_id: SCEN_ID,
    name: 'GPT-4o vs Claude',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    conversations,
  }
}

function messagesHandler(convId: string) {
  return http.get(
    `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${convId}/messages`,
    () => HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
  )
}

function baseHandlers(groupConversations?: ConversationResponse[]) {
  return [
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`, () =>
      HttpResponse.json(groupConversations ? group(groupConversations) : group()),
    ),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => HttpResponse.json(evaluation)),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/${SCEN_ID}`, () =>
      HttpResponse.json({
        id: SCEN_ID,
        evaluation_id: EVAL_ID,
        name: 'Jailbreak',
        description: 'Try hard',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      }),
    ),
    http.get(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, () =>
      HttpResponse.json({
        items: [
          {
            id: TASK_ID,
            scenario_id: SCEN_ID,
            name: 'Extract secret',
            description: 'Get the key',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 100,
        offset: 0,
      }),
    ),
    messagesHandler(CONV_A),
    messagesHandler(CONV_B),
    // The rail reads each member's completions to render its per-conversation checkboxes,
    // and derives the K/N roll-up from them.
    completedTasksHandler(CONV_A),
    completedTasksHandler(CONV_B),
    // Each pane warms up its model on mount; answer the probes ('ready' → a "Model ready" chip).
    warmupHandler(MODEL_A),
    warmupHandler(MODEL_B),
    parentGroupHandler(evaluation.evaluation_group_id),
  ]
}

function completedTasksHandler(convId: string, taskIds: string[] = []) {
  return http.get(`http://localhost/api/v1/conversations/${convId}/completed-tasks`, () => {
    completedTasksCalls += 1
    return HttpResponse.json(taskIds.map((task_id) => ({ task_id })))
  })
}

function warmupHandler(assignmentId: string) {
  return http.post(
    `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${assignmentId}/warmup`,
    () => {
      warmupCalls += 1
      return HttpResponse.json({ status: 'ready' })
    },
  )
}

function renderPage(
  perms = [
    'conversations:read',
    'conversations:participate',
    'conversations:create',
    'conversations:update',
    'conversations:delete',
  ],
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`]}>
          <Routes>
            <Route
              path="/evaluations/:id/conversation-groups/:groupId"
              element={<ConversationGroupPage />}
            />
            <Route path="/evaluations/:id" element={<div>EVAL DETAIL</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ConversationGroupPage', () => {
  it('renders a pane per member with its model name', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    expect(screen.getByText('claude-3-opus')).toBeInTheDocument()
  })

  it('renders the shared broadcast composer', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByLabelText(/message all models/i)).toBeInTheDocument())
  })

  it('blocks the broadcast while at least one model is not ready', async () => {
    server.use(...baseHandlers())
    // A warms up fine; B never becomes ready → the broadcast must stay blocked.
    // (Registered after baseHandlers so it takes precedence for MODEL_B.)
    server.use(
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${MODEL_B}/warmup`, () =>
        HttpResponse.json({ status: 'error' }),
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    // Type so the send button's only remaining gate is warmup readiness.
    await user.type(screen.getByLabelText(/message all models/i), 'hello')

    await waitFor(() => expect(screen.getByRole('button', { name: /send to all/i })).toBeDisabled())
  })

  it('renders a per-pane composer for each model', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Message gpt-4o')).toBeInTheDocument())
    expect(screen.getByLabelText('Message claude-3-opus')).toBeInTheDocument()
  })

  it('offers an open-full-conversation link per pane', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    expect(screen.getAllByRole('link', { name: /open full conversation/i })).toHaveLength(2)
  })

  it('shows the scenario and its tasks in the rail', async () => {
    server.use(...baseHandlers())
    renderPage()

    await waitFor(() => expect(screen.getByText('Jailbreak')).toBeInTheDocument())
    expect(await screen.findByText('Extract secret')).toBeInTheDocument()
  })

  it('numbers the panes and the rail rows alike when two members share a name', async () => {
    // Both models masked (absent from the evaluation's model list) → both panes and both
    // rail rows collapse to "— masked —" without a position.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, models: [] }),
      ),
      ...baseHandlers(),
    )
    const user = userEvent.setup()
    renderPage()

    // Panes carry the number...
    expect(await screen.findByLabelText('Message — masked — (1)')).toBeInTheDocument()
    expect(screen.getByLabelText('Message — masked — (2)')).toBeInTheDocument()

    // ...and the rail's rows carry the same one for the same conversation, which is what
    // makes a checkbox traceable to the pane it belongs to.
    await user.click(await screen.findByRole('button', { name: /Extract secret/ }))
    expect(
      screen.getByRole('checkbox', { name: 'Extract secret — — masked — (1)' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('checkbox', { name: 'Extract secret — — masked — (2)' }),
    ).toBeInTheDocument()
  })

  it('rolls the task up across members and derives K from their completions', async () => {
    server.use(...baseHandlers())
    // Registered after baseHandlers so it takes precedence for CONV_A.
    server.use(completedTasksHandler(CONV_A, [TASK_ID]))
    renderPage()

    // K comes from the members' own completion reads, not a separate group roll-up call.
    expect(await screen.findByLabelText('Completed in 1 of 2 conversations')).toHaveTextContent(
      '1/2',
    )
  })

  it('checks the task off in the member conversation whose box was clicked', async () => {
    let putUrl = ''
    server.use(
      ...baseHandlers(),
      http.put(
        `http://localhost/api/v1/conversations/:conversationId/completed-tasks/:taskId`,
        ({ request }) => {
          putUrl = new URL(request.url).pathname
          return HttpResponse.json({ id: 'completion-1' })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: /Extract secret/ }))
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(2) // one per member conversation
    await user.click(boxes[1]!)

    // The second member's conversation, not the group and not the first member.
    await waitFor(() =>
      expect(putUrl).toBe(`/api/v1/conversations/${CONV_B}/completed-tasks/${TASK_ID}`),
    )
  })

  it('keeps the rail read-only for a caller without conversations:update', async () => {
    server.use(...baseHandlers())
    renderPage(['conversations:read', 'conversations:participate'])

    expect(await screen.findByText('Extract secret')).toBeInTheDocument()
    // Check-off is owner-only *and* permission-gated. With nothing actionable the row is
    // just the roll-up — no disclosure, no checkboxes.
    expect(screen.queryByRole('button', { name: /Extract secret/ })).toBeNull()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByText('Read-only — not your conversations.')).toBeInTheDocument()
  })

  it('withholds task check-off from a break-glass viewer on someone else’s group', async () => {
    const OTHER_USER = 'user-0002-0000-0000-000000000000'
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () =>
          HttpResponse.json({
            ...group([
              { ...conv(CONV_A, MODEL_A), user_id: OTHER_USER },
              { ...conv(CONV_B, MODEL_B), user_id: OTHER_USER },
            ]),
            user_id: OTHER_USER,
          }),
      ),
      completedTasksHandler(CONV_A, [TASK_ID]),
      ...baseHandlers(),
    )
    renderPage(['conversations:read', 'conversations:update', 'evaluation_groups:manage'])

    expect(await screen.findByText('Extract secret')).toBeInTheDocument()
    // The break-glass lifts the owner predicate on the write services, but check-off
    // resolves the conversation under the caller's ownership with no such arm — so a
    // toggle would 404. Same reasoning as the flag control below.
    expect(screen.queryByRole('button', { name: /Extract secret/ })).toBeNull()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByText('Read-only — not your conversations.')).toBeInTheDocument()
    // Reads *are* lifted by the break-glass, so the roll-up stays real here — this viewer is
    // not the one the counts are hidden from.
    expect(await screen.findByLabelText('Completed in 1 of 2 conversations')).toBeInTheDocument()
  })

  it('shows no roll-up to a read_any supervisor, and reads no member completions', async () => {
    const OTHER_USER = 'user-0002-0000-0000-000000000000'
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:read_any',
      ]),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () =>
          HttpResponse.json({
            ...group([
              { ...conv(CONV_A, MODEL_A), user_id: OTHER_USER },
              { ...conv(CONV_B, MODEL_B), user_id: OTHER_USER },
            ]),
            user_id: OTHER_USER,
          }),
      ),
      completedTasksHandler(CONV_A, [TASK_ID]),
      ...baseHandlers(),
    )
    // No `evaluation_groups:manage`: the completions reads stay author-scoped, and authoring
    // is owner-only, so each would really answer `[]` however the handler above is stubbed.
    renderPage(['conversations:read', 'conversations:update'])

    expect(await screen.findByText('Extract secret')).toBeInTheDocument()
    // "0/2" here would report this red-teamer's work as nothing done.
    expect(screen.queryByLabelText(/Completed in/)).toBeNull()
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(
      screen.getByText("Completion progress isn't shown for conversations you don't own."),
    ).toBeInTheDocument()
    // And it doesn't spend a request per member finding that out.
    expect(completedTasksCalls).toBe(0)
  })

  it('still reads the caller’s own member of a mixed group, and keeps its checkbox live', async () => {
    const OTHER_USER = 'user-0002-0000-0000-000000000000'
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () =>
          // A break-glass manager can drop their own conversation into someone else's group
          // (the group-ownership check is lifted for them), so a group can be mixed.
          HttpResponse.json(
            group([conv(CONV_A, MODEL_A), { ...conv(CONV_B, MODEL_B), user_id: OTHER_USER }]),
          ),
      ),
      completedTasksHandler(CONV_A, [TASK_ID]),
      ...baseHandlers(),
    )
    renderPage(['conversations:read', 'conversations:update'])

    expect(await screen.findByText('Extract secret')).toBeInTheDocument()
    // The group number stays hidden — CONV_B's completions are not the caller's to read.
    expect(screen.queryByLabelText(/Completed in/)).toBeNull()
    // But their own row must not be collateral: skipping the read would render it unchecked
    // against a real completion, with a checkbox whose optimistic patch has no observer.
    await userEvent.click(screen.getByRole('button', { name: /Extract secret/ }))
    const box = screen.getByRole('checkbox')
    expect(box).toBeChecked()
    expect(box).not.toBeDisabled()
    expect(completedTasksCalls).toBeGreaterThan(0)
  })

  it('disables Add model when the scenario is gone', async () => {
    // A new member would carry the group's tombstoned scenario id, which the backend
    // refuses (live-only validation) — a dead-end action, so don't offer it.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/${SCEN_ID}`, () =>
        HttpResponse.json({ detail: 'Not Found' }, { status: 404 }),
      ),
      ...baseHandlers(),
    )
    renderPage()

    expect(await screen.findByText('Scenario no longer available.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add model/i })).toBeDisabled()
  })

  it('renames the group', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...group(), name: 'Renamed' })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /^rename$/i }))
    const input = await screen.findByLabelText('Name')
    await user.clear(input)
    await user.type(input, 'Renamed')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { name: string }).name).toBe('Renamed')
  })

  it('deletes the group and returns to the evaluation', async () => {
    let deleted = false
    server.use(
      ...baseHandlers(),
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () => {
          deleted = true
          return new HttpResponse(null, { status: 204 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /delete conversation/i }))
    await user.click(screen.getByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(deleted).toBe(true))
    await waitFor(() => expect(screen.getByText('EVAL DETAIL')).toBeInTheDocument())
  })

  it('adds a model to the group', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCEN_ID}/conversations`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(conv('conv-new', MODEL_A), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add model/i }))
    await user.click(await screen.findByLabelText('Model'))
    await user.click(await screen.findByRole('option', { name: 'gpt-4o' }))
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as {
      evaluation_ai_model_id: string
      conversation_group_id: string
      title?: string
    }
    expect(body.evaluation_ai_model_id).toBe(MODEL_A)
    expect(body.conversation_group_id).toBe(GROUP_ID)
    expect(Object.prototype.hasOwnProperty.call(body, 'title')).toBe(false)
  })

  it('includes a title when adding a model, only if set', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(),
      http.post(
        `http://localhost/api/v1/scenarios/${SCEN_ID}/conversations`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(conv('conv-new', MODEL_A), { status: 201 })
        },
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add model/i }))
    await user.click(await screen.findByLabelText('Model'))
    await user.click(await screen.findByRole('option', { name: 'gpt-4o' }))
    await user.type(screen.getByLabelText('Title (optional)'), 'Roleplay framing')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { title?: string }).title).toBe('Roleplay framing')
  })

  it('shows an inline error and keeps the dialog open when adding a model fails validation', async () => {
    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/scenarios/${SCEN_ID}/conversations`, () =>
        HttpResponse.json(
          {
            detail: 'Request body failed validation.',
            errors: [
              { loc: ['body', 'title'], msg: 'Title must not be blank.', type: 'value_error' },
            ],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /add model/i }))
    await user.click(await screen.findByLabelText('Model'))
    await user.click(await screen.findByRole('option', { name: 'gpt-4o' }))
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(await screen.findByText('Title must not be blank.')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Add model' })).toBeInTheDocument()
  })

  it('shows a conversation title in its pane, with the model name as a subtitle', async () => {
    server.use(
      ...baseHandlers([{ ...conv(CONV_A, MODEL_A), title: 'Direct ask' }, conv(CONV_B, MODEL_B)]),
    )
    renderPage()

    await waitFor(() => expect(screen.getByText('Direct ask')).toBeInTheDocument())
    expect(screen.getByLabelText('Message Direct ask (gpt-4o)')).toBeInTheDocument()
    // The model name still shows as a subtitle so the pane's identity isn't lost.
    expect(screen.getByText('gpt-4o')).toBeInTheDocument()
    // Untitled pane still falls back to the plain model name.
    expect(screen.getByLabelText('Message claude-3-opus')).toBeInTheDocument()
  })

  it('regenerates the last assistant reply from a pane', async () => {
    const regenerated: string[] = []
    server.use(
      // Only pane A has history — its pane alone should offer Regenerate/Continue.
      // (Listed before baseHandlers: the first matching handler wins, and
      // baseHandlers carries an empty-messages handler for the same route.)
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_A}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-user-0000-0000-000000000000',
                turn_id: 'turn-1',
                role: 'user',
                content: 'try this',
                status: 'complete',
                created_at: '2026-01-01T00:00:01Z',
              },
              {
                id: 'msg-asst-0000-0000-000000000000',
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'prior reply',
                status: 'complete',
                created_at: '2026-01-01T00:00:02Z',
              },
            ],
            total: 2,
            limit: 100,
            offset: 0,
          }),
      ),
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_A}/messages/:messageId/regenerate`,
        ({ params }) => {
          regenerated.push(String(params.messageId))
          return new HttpResponse('event: done\ndata: {}\n\n', {
            headers: { 'Content-Type': 'text/event-stream' },
          })
        },
      ),
      ...baseHandlers(),
    )
    renderPage()
    const regenerate = await screen.findByRole('button', { name: /regenerate/i })
    // Pane B has no messages, so exactly one pane offers the actions.
    expect(screen.getAllByRole('button', { name: /regenerate/i })).toHaveLength(1)
    await userEvent.click(regenerate)
    await waitFor(() => expect(regenerated).toEqual(['msg-asst-0000-0000-000000000000']))
  })

  it('renders the recorded tag context on a pane reply', async () => {
    // Guards the pane dropping the `tagContext={m.tag_context}` wire into `Bubble`.
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_A}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-asst-0000-0000-000000000000',
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'prior reply',
                status: 'complete',
                created_at: '2026-01-01T00:00:02Z',
                tag_context: { env: 'prod' },
              },
            ],
            total: 1,
            limit: 100,
            offset: 0,
          }),
      ),
      ...baseHandlers(),
    )
    renderPage()

    expect(
      await screen.findByTestId('tag-context-msg-asst-0000-0000-000000000000'),
    ).toHaveTextContent('env: prod')
  })

  it('shows each pane the tags its own conversation carries', async () => {
    server.use(
      ...baseHandlers([{ ...conv(CONV_A, MODEL_A), tags: { env: 'prod' } }, conv(CONV_B, MODEL_B)]),
    )
    renderPage()

    const chipRows = await waitFor(() => {
      const rows = screen.getAllByTestId('pane-conversation-tags')
      expect(rows).toHaveLength(1) // only pane A is tagged
      return rows
    })
    expect(chipRows[0]).toHaveTextContent('env: prod')
  })

  it('marks a pane chip whose key the evaluation no longer allows', async () => {
    // Same claim as the full page's: a plain chip says the model received this context. The pane had
    // the production marking and no test, so deleting it left every suite green.
    server.use(
      ...baseHandlers([
        { ...conv(CONV_A, MODEL_A), tags: { env: 'prod', legacy: 'x' } },
        conv(CONV_B, MODEL_B),
      ]),
    )
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([
          { id: 'tk-1', evaluation_id: EVAL_ID, key: 'env', created_at: '2026-01-01T00:00:00Z' },
        ]),
      ),
    )
    renderPage()

    const list = await screen.findByRole('list', { name: 'Tags for gpt-4o' })
    // The chips render before the allow-list lands, and until it settles no key may be judged — so the
    // marking is what has to be waited for, not the row.
    await waitFor(() => {
      const legacy = within(list)
        .getAllByRole('listitem')
        .find((li) => li.textContent?.includes('legacy'))
      expect(legacy).toHaveTextContent('(not sent to the model)')
    })
    const env = within(list)
      .getAllByRole('listitem')
      .find((li) => li.textContent?.startsWith('env'))
    expect(env).not.toHaveTextContent('(not sent to the model)')
  })

  it('judges a pane reply against its own record, both ways', async () => {
    // The pane carries the same reconciliation as the detail page, so it needs the same proof:
    // a key the record omits gets the marker even though the policy allows it, and a key the
    // record carries loses the marker even though the policy would not allow it.
    server.use(...baseHandlers([conv(CONV_A, MODEL_A), conv(CONV_B, MODEL_B)]))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([
          { id: 'tk-1', evaluation_id: EVAL_ID, key: 'goal', created_at: '2026-01-01T00:00:00Z' },
        ]),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_A}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-user-0000-0000-000000000000',
                turn_id: 'turn-1',
                role: 'user',
                content: 'ask',
                status: 'complete',
                created_at: '2026-01-01T00:00:01Z',
                tags: { goal: 'jailbreak', persona: 'nurse' },
              },
              {
                id: 'msg-asst-0000-0000-000000000000',
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'reply',
                status: 'complete',
                created_at: '2026-01-01T00:00:02Z',
                tag_context: { persona: 'nurse' },
              },
            ],
            total: 2,
            limit: 100,
            offset: 0,
          }),
      ),
    )
    renderPage()

    const list = await screen.findByTestId('message-tags')
    await waitFor(() => {
      const goal = within(list)
        .getAllByRole('listitem')
        .find((li) => li.textContent?.startsWith('goal'))
      expect(goal).toHaveTextContent('(not sent to the model)')
    })
    const persona = within(list)
      .getAllByRole('listitem')
      .find((li) => li.textContent?.startsWith('persona'))
    expect(persona).not.toHaveTextContent('(not sent to the model)')
  })

  it('renders no pane at all until the evaluation has resolved', async () => {
    // What lets a pane's chip marker and its row name state "tagging is off" is this guarantee: the
    // page holds the panes back until it knows the evaluation, so `tags_enabled=false` there can only
    // mean off, never "not read yet". Without it the pane would need its own settled-ness signal.
    server.use(
      ...baseHandlers([{ ...conv(CONV_A, MODEL_A), tags: { env: 'prod' } }, conv(CONV_B, MODEL_B)]),
    )
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderPage()

    // Anchor on a settled page first: both assertions below are absences, and on the first tick
    // nothing is rendered at all, so `waitFor` would resolve at t=0 whatever the page does next.
    // Note the limit of the negative form: with the gate removed the pane subtree throws on the
    // undefined evaluation, so the commit fails and *nothing* renders — the suite goes red on the
    // unhandled error rather than on these assertions. Measured, not assumed. Do not "simplify" the
    // anchor away: without it this test passes at t=0 and reports nothing at all.
    await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull())
    expect(screen.queryAllByTestId('pane-conversation-tags')).toHaveLength(0)
    expect(screen.queryByLabelText('Message gpt-4o')).toBeNull()
  })

  it('names each pane tag row after its own pane', async () => {
    // Side by side, several rows all called "Tags" are indistinguishable in a reader's element list.
    server.use(
      ...baseHandlers([
        { ...conv(CONV_A, MODEL_A), tags: { env: 'prod' } },
        { ...conv(CONV_B, MODEL_B), tags: { env: 'dev' } },
      ]),
    )
    renderPage()

    await waitFor(() => expect(screen.getAllByTestId('pane-conversation-tags')).toHaveLength(2))
    const names = screen
      .getAllByRole('list', { name: /^Tags for / })
      .map((el) => el.getAttribute('aria-label'))
    expect(new Set(names).size).toBe(2)
  })

  it('exposes a pane chip row as a labelled list, one item per tag', async () => {
    // On a bare <div> the `aria-label` reaches no screen reader — naming is not allowed on a generic
    // role — so the chips read as loose text in the pane header. The full conversation page spells the
    // list role out for the same reason.
    server.use(
      ...baseHandlers([
        { ...conv(CONV_A, MODEL_A), tags: { env: 'prod', region: 'eu-west' } },
        conv(CONV_B, MODEL_B),
      ]),
    )
    renderPage()

    // Named per pane so two rows are distinguishable in a reader's element list.
    const list = await screen.findByRole('list', { name: 'Tags for gpt-4o' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
  })

  it('keeps a long tag value reachable without letting it take the pane', async () => {
    // Uncapped, a 512-char value wraps to four lines in a pane header and pushes the transcript down;
    // the full page caps and titles its chips for exactly this reason.
    const long = 'x'.repeat(400)
    server.use(
      ...baseHandlers([{ ...conv(CONV_A, MODEL_A), tags: { note: long } }, conv(CONV_B, MODEL_B)]),
    )
    renderPage()

    expect(await screen.findByTitle(`note: ${long}`)).toBeInTheDocument()
  })

  it("feeds every pane's key picker from one shared allow-list request", async () => {
    // What this actually guards is the query *key*, not the call site: `useEvaluationTagKeys` keys on
    // ['evaluation-tag-keys', evaluationId], so N panes calling the hook already share one cache
    // entry and one fetch. A single request proves the key stayed evaluation-scoped — a regression to
    // a per-conversation key, or a staleTime that stopped de-duplicating, is what would break it.
    let tagKeyCalls = 0
    server.use(...baseHandlers())
    // Registered after the base handlers so the restricted evaluation wins.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () => {
        tagKeyCalls += 1
        return HttpResponse.json([
          {
            id: 'tk-1',
            evaluation_id: EVAL_ID,
            key: 'env',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ])
      }),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    const toggles = screen.getAllByRole('button', { name: /^tags/i })
    expect(toggles).toHaveLength(2)
    await user.click(toggles[0]!)

    // Restricted, so the key half is a select limited to the allowed keys.
    const picker = await screen.findByLabelText('Message tag 1 key')
    expect(picker).toHaveTextContent('— select key —')
    await user.click(picker)
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      '— select key —',
      'env',
    ])
    expect(tagKeyCalls).toBe(1)
  })

  it('leaves the allow-list unfetched while the evaluation keeps tags free-form', async () => {
    let tagKeyCalls = 0
    server.use(...baseHandlers())
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () => {
        tagKeyCalls += 1
        return HttpResponse.json([])
      }),
    )
    renderPage()

    await waitFor(() => expect(screen.getByText('gpt-4o')).toBeInTheDocument())
    expect(tagKeyCalls).toBe(0)
  })
})

describe('ConversationGroupPage — the way back to the evaluation group', () => {
  it('names the evaluation group this conversation sits in, and links to it', async () => {
    server.use(
      http.get('http://localhost/api/v1/evaluation-groups/grp-0000-0000-0000-000000000000', () =>
        HttpResponse.json({
          id: 'grp-0000-0000-0000-000000000000',
          title: 'Spring Jailbreak Sprint',
          description: null,
          status: 'published',
          access_level: 'public',
          organization_id: null,
          created_by_id: 'user-0001',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      ),
      ...baseHandlers(),
    )
    renderPage()

    // Before this the trail started at the evaluation, so the group was unreachable from here.
    const identity = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(
      await within(identity).findByRole('link', { name: 'Spring Jailbreak Sprint' }),
    ).toHaveAttribute('href', '/evaluation-groups/grp-0000-0000-0000-000000000000')
  })
})

describe('ConversationGroupPage — in-group authority', () => {
  it('renders the panes for a group-scoped red teamer with no global conversation permission', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:participate',
      ]),
      // Each pane fetches its own transcript, so assert on a message too — the group read
      // succeeding says nothing about whether the panes' reads carry the same authority.
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_A}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-0001-0000-0000-000000000000',
                conversation_id: CONV_A,
                role: 'user',
                content: 'in-group transcript line',
                status: 'complete',
                created_at: '2026-01-01T00:00:00Z',
                updated_at: '2026-01-01T00:00:00Z',
              },
            ],
            total: 1,
            limit: 100,
            offset: 0,
          }),
      ),
      ...baseHandlers(),
    )
    renderPage(['evaluations:read'])

    expect(await screen.findByText('gpt-4o')).toBeInTheDocument()
    expect(screen.getByText('claude-3-opus')).toBeInTheDocument()
    expect(await screen.findByText('in-group transcript line')).toBeInTheDocument()
  })

  it('refuses when neither the JWT nor the in-group role grants read', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read'])

    expect(await screen.findByText(/access to this section/i)).toBeInTheDocument()
  })
})

describe('ConversationGroupPage — a member group surfaced by read_any', () => {
  it('shows the panes but no write affordances on another member group', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:read_any',
      ]),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () => HttpResponse.json({ ...group(), user_id: 'someone-else-0000-0000-000000000000' }),
      ),
      ...baseHandlers(),
    )
    // Global write permissions on purpose: `read_any` widens reads only.
    renderPage([
      'conversations:read',
      'conversations:create',
      'conversations:update',
      'conversations:delete',
    ])

    expect(await screen.findByText('gpt-4o')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /add model/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^rename$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete conversation/i })).toBeNull()
    expect(screen.queryByLabelText(/message all models/i)).toBeNull()
    // Each pane carries its own composer, so hiding the broadcast alone would leave N send boxes.
    expect(screen.queryByLabelText('Message gpt-4o')).toBeNull()
    expect(screen.queryByLabelText('Message claude-3-opus')).toBeNull()
    // Warming is a write on the warmup route; probing anyway would report the 403 per pane as the
    // model being unavailable.
    expect(warmupCalls).toBe(0)
  })

  it('keeps the write affordances for a break-glass admin on another member group', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, ['conversations:read']),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () => HttpResponse.json({ ...group(), user_id: 'someone-else-0000-0000-000000000000' }),
      ),
      ...baseHandlers(),
    )
    // `evaluation_groups:manage` lifts the owner predicate on the write services, so hiding these
    // would take away something that works.
    renderPage([
      'conversations:read',
      'conversations:create',
      'conversations:update',
      'conversations:delete',
      'evaluation_groups:manage',
    ])

    expect(await screen.findByText('gpt-4o')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^rename$/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/message all models/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Message gpt-4o')).toBeInTheDocument()
  })
})

describe('ConversationGroupPage — labels and flags in a pane', () => {
  const MSG_ID = 'msg-pane1-0000-0000-000000000000'
  const OTHER_USER = 'user-0002-0000-0000-000000000000'
  const LABEL = { id: 'label-jb', key: 'jailbreak', name: 'Jailbreak', is_custom: false }

  // Only the pane asked for gets a message, so anything found on screen belongs to that pane
  // rather than being fanned out to every member.
  function paneMessage(convId: string, flagCount = 0) {
    return http.get(
      `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${convId}/messages`,
      () =>
        HttpResponse.json({
          items: [
            {
              id: MSG_ID,
              turn_id: 'turn-1',
              role: 'assistant',
              content: 'Here is the recipe.',
              status: 'complete',
              flag_count: flagCount,
              tag_context_partial: false,
              created_at: '2026-01-01T00:00:01Z',
            },
          ],
          total: 1,
          limit: 100,
          offset: 0,
        }),
    )
  }

  // Each pane queries for its own conversation, so the handler answers per `conversation_id` —
  // returning the label for every pane would hide a leak between them.
  function annotationsFor(convId: string) {
    return http.get('http://localhost/api/v1/annotations', ({ request }) => {
      const asked = new URL(request.url).searchParams.get('conversation_id')
      const items =
        asked === convId
          ? [
              {
                id: 'ann-1',
                message_id: MSG_ID,
                conversation_id: convId,
                label: LABEL,
                created_by_id: OTHER_USER,
                evaluation_id: EVAL_ID,
                evaluation_group_id: evaluation.evaluation_group_id,
                created_at: '2026-08-27T12:00:00Z',
                updated_at: '2026-08-27T12:00:00Z',
              },
            ]
          : []
      return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
    })
  }

  it('shows another annotator’s label on the message it belongs to', async () => {
    server.use(paneMessage(CONV_A), annotationsFor(CONV_A), ...baseHandlers())
    renderPage(['conversations:read', 'annotations:read'])

    const chips = await screen.findAllByTestId('message-annotations')
    expect(chips).toHaveLength(1)
    expect(within(chips[0]!).getByText('Jailbreak')).toBeInTheDocument()
  })

  it('hides labels entirely from a caller holding no annotation key', async () => {
    // No handler for the annotations route: with the key absent the query must never run, and
    // `onUnhandledRequest: 'error'` fails this test if it does.
    server.use(paneMessage(CONV_A), ...baseHandlers())
    renderPage(['conversations:read'])

    expect(await screen.findByText('Here is the recipe.')).toBeInTheDocument()
    expect(screen.queryByTestId('message-annotations')).toBeNull()
    expect(screen.queryByRole('button', { name: /label this message/i })).toBeNull()
  })

  it('offers the label affordance with annotations:create', async () => {
    server.use(paneMessage(CONV_A), annotationsFor(CONV_A), ...baseHandlers())
    renderPage(['conversations:read', 'annotations:read', 'annotations:create'])

    expect(await screen.findByRole('button', { name: /label this message/i })).toBeInTheDocument()
  })

  it('opens the label dialog for the message the button belongs to', async () => {
    server.use(
      paneMessage(CONV_A),
      annotationsFor(CONV_A),
      http.get('http://localhost/api/v1/annotation-labels', () =>
        HttpResponse.json({ items: [LABEL], total: 1, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
    )
    const user = userEvent.setup()
    renderPage(['conversations:read', 'annotations:read', 'annotations:create'])

    await user.click(await screen.findByRole('button', { name: /label this message/i }))

    expect(await screen.findByRole('heading', { name: /label this message/i })).toBeInTheDocument()
    // The dialog received this message's annotations, not an empty set — the colleague's label
    // shows in the read-only section, since only the caller's own are removable.
    expect(screen.getByText(/also labelled by other annotators/i)).toBeInTheDocument()
  })

  it('opens the flag dialog from a pane bubble', async () => {
    server.use(paneMessage(CONV_A), ...baseHandlers())
    const user = userEvent.setup()
    renderPage(['conversations:read', 'flags:create'])

    await user.click(await screen.findByRole('button', { name: /flag this reply for review/i }))

    expect(await screen.findByRole('heading', { name: /^flag message$/i })).toBeInTheDocument()
  })

  it('reports a failed label read instead of showing the message as unlabelled', async () => {
    server.use(
      http.get(
        'http://localhost/api/v1/annotations',
        () => new HttpResponse(null, { status: 500 }),
      ),
      paneMessage(CONV_A),
      ...baseHandlers(),
    )
    renderPage(['conversations:read', 'annotations:read'])

    // One per pane: each reads its own conversation, so each reports its own failure.
    expect(await screen.findAllByText(/labels are unavailable/i)).toHaveLength(2)
  })

  it('says so when a pane holds more annotations than one page', async () => {
    // The read is one page newest-first, so past the cap the oldest-annotated messages show no
    // chips — indistinguishable from unlabelled, which is what the failure surface above guards.
    server.use(
      http.get('http://localhost/api/v1/annotations', () =>
        HttpResponse.json({ items: [], total: 250, limit: 100, offset: 0 }),
      ),
      paneMessage(CONV_A),
      ...baseHandlers(),
    )
    renderPage(['conversations:read', 'annotations:read'])

    expect(await screen.findAllByText(/showing the 100 most recent labels of 250/i)).toHaveLength(2)
  })

  it('badges a message the caller has already flagged', async () => {
    server.use(paneMessage(CONV_A, 2), ...baseHandlers())
    renderPage(['conversations:read'])

    expect(await screen.findByText(/2 flagged/)).toBeInTheDocument()
  })

  it('offers flagging on an assistant reply in a conversation the caller owns', async () => {
    server.use(paneMessage(CONV_A), ...baseHandlers())
    renderPage(['conversations:read', 'flags:create'])

    expect(
      await screen.findByRole('button', { name: /flag this reply for review/i }),
    ).toBeInTheDocument()
  })

  it('withholds flagging from a break-glass viewer on someone else’s conversation', async () => {
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
        () =>
          HttpResponse.json({
            ...group([
              { ...conv(CONV_A, MODEL_A), user_id: OTHER_USER },
              { ...conv(CONV_B, MODEL_B), user_id: OTHER_USER },
            ]),
            user_id: OTHER_USER,
          }),
      ),
      paneMessage(CONV_A),
      ...baseHandlers(),
    )
    renderPage(['conversations:read', 'flags:create', 'evaluation_groups:manage'])

    expect(await screen.findByText('Here is the recipe.')).toBeInTheDocument()
    // The break-glass lifts the owner predicate on the write services, but the flag services
    // filter on the conversation's own `user_id` with no such arm — so the control would 404.
    expect(screen.getByLabelText('Message gpt-4o')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /flag this reply for review/i })).toBeNull()
  })
})
