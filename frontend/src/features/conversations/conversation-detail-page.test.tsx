import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { AUTH_USER_ID, authWrapper } from '@/lib/auth/auth.testutils'
import { parentGroupHandler } from './test-fixtures'
import { ConversationDetailPage } from './conversation-detail-page'
import type { EvaluationResponse, ConversationResponse } from '@/lib/api/types'
import { licenseStub } from '@/features/licenses/test-fixtures'
import { MAX_TAGS } from '@/lib/api/limits'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const CONV_ID = 'conv-0001-0000-0000-000000000000'
const MODEL_ID = 'model-001-0000-0000-000000000000'
const SCENARIO_ID = 's1-00001-0000-0000-000000000000'
const TOMBSTONED_SCENARIO_ID = 's2-00002-0000-0000-000000000000'
const GROUP_ID = 'grp-0001-0000-0000-000000000000'

// Counted, not just answered: a persona that may not write must never probe the warmup route.
let warmupCalls = 0
beforeEach(() => {
  warmupCalls = 0
})

function groupHandler(name = 'Model probe') {
  return http.get(
    `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`,
    () =>
      HttpResponse.json({
        id: GROUP_ID,
        user_id: AUTH_USER_ID,
        evaluation_id: EVAL_ID,
        name,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        conversations: [],
      }),
  )
}

const evaluation: EvaluationResponse = {
  id: EVAL_ID,
  evaluation_group_id: 'grp-0000-0000-0000-000000000000',
  created_by_id: 'user-000-0000-0000-000000000000',
  title: 'Test eval',
  description: 'desc',
  status: 'approved',
  mask_models_enabled: false,
  tags_enabled: true,
  tags_restricted: false,
  effective_license: licenseStub('CC-BY-4.0'),
  models: [
    {
      assignment_id: MODEL_ID,
      name: 'gpt-4o',
      warmup_enabled: true,
      advanced_params_disabled: false,
      input_modalities: ['text', 'image'],
    },
  ],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const conversationWithScenario: ConversationResponse = {
  id: CONV_ID,
  user_id: AUTH_USER_ID,
  evaluation_id: EVAL_ID,
  evaluation_ai_model_id: MODEL_ID,
  content_protected: false,
  scenario_id: SCENARIO_ID,
  conversation_group_id: 'grp-0001-0000-0000-000000000000',
  effective_license: licenseStub('CC-BY-4.0'),
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

// Points at a scenario the by-id read 404s (tombstoned) — the page then shows a
// tombstone note instead of the rail and skips the tasks fetch.
const conversationTombstonedScenario: ConversationResponse = {
  id: CONV_ID,
  user_id: AUTH_USER_ID,
  evaluation_id: EVAL_ID,
  evaluation_ai_model_id: MODEL_ID,
  content_protected: false,
  scenario_id: TOMBSTONED_SCENARIO_ID,
  conversation_group_id: 'grp-0001-0000-0000-000000000000',
  effective_license: licenseStub('CC-BY-4.0'),
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const conversationWithTitle: ConversationResponse = {
  ...conversationTombstonedScenario,
  title: 'Direct ask',
}

const conversationWithTags: ConversationResponse = {
  ...conversationTombstonedScenario,
  tags: { env: 'prod' },
}

function baseHandlers(conv: ConversationResponse) {
  return [
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
      HttpResponse.json(conv),
    ),
    http.get(
      `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
      () => HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => HttpResponse.json(evaluation)),
    groupHandler(),
    parentGroupHandler(evaluation.evaluation_group_id),
    // The model is warmup_enabled, so the page probes it on mount; answer the
    // probe (MSW errors on unhandled requests). 'ready' → the badge shows "Model ready".
    http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${MODEL_ID}/warmup`, () => {
      warmupCalls += 1
      return HttpResponse.json({ status: 'ready' })
    }),
    // The page resolves the conversation's scenario by id; the read is live-only, so
    // anything but SCENARIO_ID is a tombstone (404).
    http.get(
      `http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/:scenarioId`,
      ({ params }) =>
        params.scenarioId === SCENARIO_ID
          ? HttpResponse.json({
              id: SCENARIO_ID,
              name: 'Prompt injection',
              description: 'Coax a restricted answer',
              evaluation_id: EVAL_ID,
              position: 0,
            })
          : HttpResponse.json({ detail: 'Not Found' }, { status: 404 }),
    ),
  ]
}

function renderPage() {
  // The page's reads are gated on `conversations:read`, so the wrapper has to carry it or every
  // query stays disabled and nothing renders.
  return renderWithPerms(['conversations:read', 'conversations:update'])
}

describe('ConversationDetailPage — composer attachments', () => {
  const realCreate = URL.createObjectURL
  const realRevoke = URL.revokeObjectURL
  beforeAll(() => {
    let n = 0
    URL.createObjectURL = vi.fn(() => `blob:test-${n++}`)
    URL.revokeObjectURL = vi.fn()
  })
  afterAll(() => {
    URL.createObjectURL = realCreate
    URL.revokeObjectURL = realRevoke
  })

  it('uploads a picked image, sends its key with the message, and clears the strip', async () => {
    const sseBodies: Record<string, unknown>[] = []
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json(
          {
            id: 'img-0001-0000-0000-000000000000',
            key: '2026/07/17/pick.png',
            url: '/api/v1/images/2026/07/17/pick.png',
            content_type: 'image/png',
            width: 1,
            height: 1,
            size_bytes: 1,
            is_private: true,
            created_at: '2026-07-17T00:00:00Z',
          },
          { status: 201 },
        ),
      ),
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        async ({ request }) => {
          sseBodies.push((await request.json()) as Record<string, unknown>)
          return new HttpResponse('event: done\ndata: {}\n\n', {
            headers: { 'Content-Type': 'text/event-stream' },
          })
        },
      ),
    )
    renderPage()
    const sendButton = await screen.findByRole('button', { name: 'Send' })

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(fileInput, new File(['x'], 'pick.png', { type: 'image/png' }))
    await screen.findByAltText('pick.png') // preview thumb in the strip
    await waitFor(() => expect(screen.getByText('1/5')).toBeInTheDocument())

    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'look at this')
    await waitFor(() => expect(sendButton).toBeEnabled()) // upload settled
    await userEvent.click(sendButton)

    await waitFor(() => expect(sseBodies).toHaveLength(1))
    expect(sseBodies[0]?.content).toBe('look at this')
    expect(sseBodies[0]?.image_keys).toEqual(['2026/07/17/pick.png'])
    // Sent successfully → the strip resets for the next message.
    await waitFor(() => expect(screen.queryByAltText('pick.png')).not.toBeInTheDocument())
    expect(screen.queryByText('1/5')).not.toBeInTheDocument()
  })

  it('drops the attached image into the conversation on send and clears the strip immediately', async () => {
    let release!: () => void
    const streamGate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json(
          {
            id: 'img-0001-0000-0000-000000000000',
            key: '2026/07/17/pick.png',
            url: '/api/v1/images/2026/07/17/pick.png',
            content_type: 'image/png',
            width: 1,
            height: 1,
            size_bytes: 1,
            is_private: true,
            created_at: '2026-07-17T00:00:00Z',
          },
          { status: 201 },
        ),
      ),
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () => {
          // Hold the stream open so the in-flight (optimistic) render is observable.
          const stream = new ReadableStream({
            async start(controller) {
              controller.enqueue(
                new TextEncoder().encode('event: delta\ndata: {"content":"…"}\n\n'),
              )
              await streamGate
              controller.enqueue(new TextEncoder().encode('event: done\ndata: {}\n\n'))
              controller.close()
            },
          })
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } })
        },
      ),
    )
    renderPage()
    const sendButton = await screen.findByRole('button', { name: 'Send' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(fileInput, new File(['x'], 'pick.png', { type: 'image/png' }))
    await screen.findByAltText('pick.png')
    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'look at this')
    await waitFor(() => expect(sendButton).toBeEnabled())
    await userEvent.click(sendButton)

    // While the reply is still streaming: the image is already shown in the conversation…
    await waitFor(() => expect(screen.getByAltText('Attachment 1')).toBeInTheDocument())
    // …and the composer strip has been cleared, not left holding the thumbnail.
    expect(screen.queryByAltText('pick.png')).not.toBeInTheDocument()
    expect(screen.queryByText('1/5')).not.toBeInTheDocument()
    release()
  })

  it('keeps the text and the attachment in the composer when the send fails', async () => {
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json(
          {
            id: 'img-0001-0000-0000-000000000000',
            key: '2026/07/17/pick.png',
            url: '/api/v1/images/2026/07/17/pick.png',
            content_type: 'image/png',
            width: 1,
            height: 1,
            size_bytes: 1,
            is_private: true,
            created_at: '2026-07-17T00:00:00Z',
          },
          { status: 201 },
        ),
      ),
      // Pre-persist rejection: the turn never reaches the model, so nothing is saved.
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () => HttpResponse.json({ title: 'Unprocessable' }, { status: 422 }),
      ),
    )
    renderPage()
    const sendButton = await screen.findByRole('button', { name: 'Send' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(fileInput, new File(['x'], 'pick.png', { type: 'image/png' }))
    await screen.findByAltText('pick.png')
    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'look at this')
    await waitFor(() => expect(sendButton).toBeEnabled())
    await userEvent.click(sendButton)

    // Failed send → the text and the uploaded image are still in the composer, not lost.
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('look at this'),
    )
    expect(screen.getByAltText('pick.png')).toBeInTheDocument()
    expect(screen.getByText('1/5')).toBeInTheDocument()
  })

  it('hides the attach affordance when the model does not accept images', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    // Later use() wins over baseHandlers' eval handler: a non-vision model.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({
          ...evaluation,
          models: [
            {
              assignment_id: MODEL_ID,
              name: 'gpt-4o',
              warmup_enabled: true,
              input_modalities: ['text'],
            },
          ],
        }),
      ),
    )
    renderPage()
    // Composer is up (Send present) but no attach button — a non-vision model can't take images.
    await screen.findByRole('button', { name: 'Send' })
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Attach images' })).not.toBeInTheDocument(),
    )
    expect(document.querySelector('input[type="file"]')).toBeNull()
  })
})

describe('ConversationDetailPage — scenario header', () => {
  it('shows scenario name and description when conversation has a matching scenario_id', async () => {
    server.use(...baseHandlers(conversationWithScenario))
    renderPage()

    await waitFor(() => expect(screen.getByText('Prompt injection')).toBeInTheDocument())
    expect(screen.getByText('Coax a restricted answer')).toBeInTheDocument()
  })

  it('shows a tombstone note when the scenario is not in the live list', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderPage()

    expect(await screen.findByText('Scenario no longer available.')).toBeInTheDocument()
    expect(screen.queryByText('Prompt injection')).toBeNull()
    expect(screen.queryByText(/loading scenario/i)).toBeNull()
  })
})

describe('ConversationDetailPage — title', () => {
  it('shows the conversation title as the heading and breadcrumb when set', async () => {
    server.use(...baseHandlers(conversationWithTitle))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Direct ask' })).toBeInTheDocument(),
    )
    expect(screen.getAllByText('Direct ask').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: 'Single conversation' })).toBeNull()
  })

  it('falls back to the generic heading when no title is set', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
  })
})

describe('ConversationDetailPage — breadcrumb', () => {
  it('shows the parent session (group) as a linked crumb', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderPage()

    const crumb = await screen.findByRole('link', { name: 'Model probe' })
    expect(crumb).toHaveAttribute('href', `/evaluations/${EVAL_ID}/conversation-groups/${GROUP_ID}`)
  })
})

function renderWithPerms(perms: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/evaluations/${EVAL_ID}/conversations/${CONV_ID}`]}>
          <Routes>
            <Route
              path="/evaluations/:id/conversations/:conversationId"
              element={<ConversationDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ConversationDetailPage — rename', () => {
  it('patches the conversation with the new title', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, title: 'Direct ask' })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /^rename$/i }))
    await user.type(await screen.findByLabelText('Title'), 'Direct ask')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { title: string | null }).title).toBe('Direct ask')
  })

  it('clears the title by sending explicit null when the field is emptied', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationWithTitle),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationWithTitle, title: null })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Direct ask' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /^rename$/i }))
    const input = await screen.findByLabelText('Title')
    await user.clear(input)
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { title: string | null }).title).toBeNull()
  })

  it('shows an inline error and keeps the dialog open when the rename fails validation', async () => {
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
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
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /^rename$/i }))
    await user.type(await screen.findByLabelText('Title'), 'x')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByText('Title must not be blank.')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Rename single conversation' })).toBeInTheDocument()
  })

  it('does not offer Rename without conversations:update', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^rename$/i })).toBeNull()
  })
})

describe('ConversationDetailPage — tags', () => {
  it('renders tag chips and labels the button "Edit tags" when tags exist', async () => {
    server.use(...baseHandlers(conversationWithTags))
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(screen.getByText('env: prod')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /edit tags/i })).toBeInTheDocument()
  })

  it('renders the chip in the case the operator authored', async () => {
    server.use(
      ...baseHandlers({
        ...conversationTombstonedScenario,
        tags: { persona: 'Dr. Smith from Acme Labs' },
      }),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    // No stylesheet in jsdom, so the class is the only observable here: an uppercasing variant
    // mangles operator-authored free text, which is stored and sent as typed. (CSS casing does
    // not reach the accessible name — the class list is the whole assertion.)
    // `getByTitle` lands on the chip itself, the element that carries the variant.
    expect(screen.getByTitle('persona: Dr. Smith from Acme Labs')).not.toHaveClass('uppercase')
  })

  it('exposes the chip row as a labelled list, one item per tag', async () => {
    server.use(
      ...baseHandlers({
        ...conversationTombstonedScenario,
        tags: { env: 'prod', region: 'eu-west' },
      }),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    const list = screen.getByRole('list', { name: 'Tags' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
  })

  it('keeps the whole tag text reachable when the chip is capped', async () => {
    const long = 'x'.repeat(400)
    server.use(...baseHandlers({ ...conversationTombstonedScenario, tags: { note: long } }))
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    // The chip is width-capped, so the full value has to stay available without opening the dialog.
    expect(screen.getByTitle(`note: ${long}`)).toBeInTheDocument()
  })

  it('shows an "Add tags" button and no chips when there are no tags', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /add tags/i })).toBeInTheDocument()
    expect(screen.queryByTestId('conversation-tags')).toBeNull()
  })

  it('adds a tag and PATCHes the whole map', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, tags: { env: 'prod' } })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { tags: Record<string, string> }).tags).toEqual({ env: 'prod' })
  })

  it('drops blank-key rows before sending', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, tags: { env: 'prod' } })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^add tag$/i })) // a second, left-blank row
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { tags: Record<string, string> }).tags).toEqual({ env: 'prod' }) // blank row dropped
  })

  it('names the row and sends nothing when a value was typed without a key', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, tags: {} })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Tag 2 value'), 'never respond in English')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByText('Row 2: key is required')).toBeInTheDocument()
    expect(captured).toBeNull()
    expect(screen.getByLabelText('Tag 2 value')).toHaveValue('never respond in English')
  })

  it('names the colliding key and sends nothing when two rows share one', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, tags: {} })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Tag 2 key'), ' env ') // trimmed, so it collides
    await user.type(screen.getByLabelText('Tag 2 value'), 'staging')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByText('Duplicate key "env"')).toBeInTheDocument()
    expect(captured).toBeNull()
  })

  it('shows the saved tags without a reload', async () => {
    // A mutable fixture, so the chips can only appear if the save invalidated the conversation query.
    let current: ConversationResponse = { ...conversationTombstonedScenario }
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
        HttpResponse.json(current),
      ),
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          const body = (await request.json()) as { tags: Record<string, string> }
          current = { ...current, tags: body.tags }
          return HttpResponse.json(current)
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('conversation-tags')).toBeNull()

    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByTitle('env: prod')).toBeInTheDocument()
  })

  it('leaves the caret in the row being edited when a row above it is removed', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'a')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Tag 2 key'), 'b')
    await user.type(screen.getByLabelText('Tag 2 value'), 'second')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Tag 3 key'), 'c')

    // Index keys make React reuse row 1's inputs for row 2's data on removal, so anything anchored to
    // the node — the caret, an IME composition, a native tooltip — follows the wrong row.
    const editing = screen.getByLabelText('Tag 2 value')
    await user.click(screen.getByRole('button', { name: /^remove tag 1$/i }))

    expect(screen.getByLabelText('Tag 1 value')).toBe(editing)
    expect(editing).toHaveValue('second')
  })

  it('caps the key and value fields at the lengths the API accepts', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))

    expect(await screen.findByLabelText('Tag 1 key')).toHaveAttribute('maxlength', '64')
    expect(screen.getByLabelText('Tag 1 value')).toHaveAttribute('maxlength', '512')
  })

  it('puts the caret in the first key field on open', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))

    expect(await screen.findByLabelText('Tag 1 key')).toHaveFocus()
  })

  it('stops offering new rows at the tag cap', async () => {
    const full = Object.fromEntries(Array.from({ length: MAX_TAGS }, (_, i) => [`k${i}`, 'v']))
    server.use(...baseHandlers({ ...conversationTombstonedScenario, tags: full }))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))

    // `aria-disabled`, not `disabled`: the control stays in the tab order so a keyboard user can reach
    // it and hear the reason, and the handler refuses instead — so the row count is what proves it.
    const addTag = await screen.findByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    expect(
      screen.getByText(new RegExp(`up to ${MAX_TAGS} tags per conversation`, 'i')),
    ).toBeInTheDocument()

    await user.click(addTag)

    expect(screen.queryByLabelText(`Tag ${MAX_TAGS + 1} key`)).toBeNull()
  })

  it('does not offer a save until something actually changed', async () => {
    server.use(...baseHandlers(conversationWithTags))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))

    // Saving an untouched dialog would PATCH the same map back and toast as if something happened.
    expect(await screen.findByRole('button', { name: /^save$/i })).toBeDisabled()
    await user.type(screen.getByLabelText('Tag 1 value'), '-eu')
    expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled()
  })

  it('does not offer a save on an untouched empty tag set', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))

    // The blank seeded row is not an edit — saving it would send `{}` and read as a deliberate clear.
    expect(await screen.findByRole('button', { name: /^save$/i })).toBeDisabled()
  })

  it.each(['constructor', 'toString', '__proto__'])('saves %s as an ordinary key', async (key) => {
    // `k in tags` on an object literal is true for every `Object.prototype` member, so these keys —
    // all of which pass the published pattern — were reported as duplicates on first use. `__proto__`
    // was worse: assigning it on a literal hits the inherited setter, so the row left the payload
    // entirely while the dialog reported success.
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationTombstonedScenario, tags: { [key]: 'x' } })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), key)
    await user.type(screen.getByLabelText('Tag 1 value'), 'never respond in English')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).toEqual({ tags: { [key]: 'never respond in English' } }))
    expect(screen.queryByText(/duplicate key/i)).toBeNull()
  })

  it('does not offer a save when an added row normalises away', async () => {
    server.use(...baseHandlers(conversationWithTags))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    await user.click(await screen.findByRole('button', { name: /^add tag$/i }))

    // The added row is blank, so the map that would be sent is byte-identical to the stored one.
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled()
  })

  it('does not offer a save when only whitespace around a key changed', async () => {
    server.use(...baseHandlers(conversationWithTags))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    const keyField = await screen.findByLabelText('Tag 1 key')
    await user.type(keyField, ' ')

    // Keys are trimmed on the way into the map, so this is not an edit either.
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled()
  })

  it('still offers a save when a row cannot build, so the reason gets reported', async () => {
    server.use(...baseHandlers(conversationWithTags))
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    await user.click(await screen.findByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Tag 2 value'), 'never respond in English')

    // Deriving `dirty` from the built map must not disable Save on input that fails to build, or the
    // operator would be stuck with no explanation.
    expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /^save$/i }))
    expect(await screen.findByText('Row 2: key is required')).toBeInTheDocument()
  })

  it('clears all tags (sends {}) when the only row is removed', async () => {
    let captured: unknown = null
    server.use(
      ...baseHandlers(conversationWithTags),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...conversationWithTags, tags: {} })
        },
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() => expect(screen.getByText('env: prod')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    await user.click(await screen.findByRole('button', { name: /remove tag 1/i }))
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { tags: Record<string, string> }).tags).toEqual({})
  })

  it('shows an inline error and keeps the dialog open when tags fail validation', async () => {
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
        HttpResponse.json(
          {
            detail: 'Request body failed validation.',
            errors: [{ loc: ['body', 'tags'], msg: 'invalid tag key', type: 'value_error' }],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'bad key')
    await user.type(screen.getByLabelText('Tag 1 value'), 'v')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByText('invalid tag key')).toBeInTheDocument()
    // No tags yet on this conversation, so the dialog is titled to match the "Add tags" entry point.
    expect(screen.getByRole('dialog', { name: 'Add tags' })).toBeInTheDocument()
  })

  it('offers no tag authoring when the evaluation has tagging disabled', async () => {
    server.use(...baseHandlers(conversationWithTags))
    // Registered after the base handlers so it wins: same evaluation, tagging switched off.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_enabled: false }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /(add|edit) tags/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^tags/i })).toBeNull() // the composer's toggle
  })

  it('keeps showing the tags a conversation already carries once tagging is disabled', async () => {
    // The flag governs authoring and the prompt fold, not the record: the backend keeps stored tags
    // and stops folding them in, so hiding them here would lose the context a turn was sent with —
    // and a reviewer reading the transcript has no other way to see it.
    server.use(...baseHandlers(conversationWithTags))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_enabled: false }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    expect(await screen.findByTestId('conversation-tags')).toBeInTheDocument()
    expect(screen.getByText('env: prod')).toBeInTheDocument()
    expect(screen.getByText(/kept but not sent to the model/i)).toBeInTheDocument()
  })

  it('does not claim stored tags reach the model while tagging is enabled', async () => {
    server.use(...baseHandlers(conversationWithTags))
    renderWithPerms(['conversations:read', 'conversations:update'])

    expect(await screen.findByTestId('conversation-tags')).toBeInTheDocument()
    expect(screen.queryByText(/kept but not sent to the model/i)).toBeNull()
  })

  it('names a stored tag whose key the evaluation no longer allows', async () => {
    // The server folds only the keys a restricted evaluation currently allows and logs the rest as
    // dropped, so a plain chip here says "the model saw this" about context it never received — while
    // the dialog one click away labels the same key "(no longer allowed)".
    server.use(
      ...baseHandlers({ ...conversationTombstonedScenario, tags: { env: 'prod', legacy: 'x' } }),
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
    renderWithPerms(['conversations:read', 'conversations:update'])

    expect(await screen.findByText(/kept but not sent to the model: legacy/i)).toBeInTheDocument()
    // Marked per chip with text a reader gets, not with opacity or a `title`.
    const items = within(screen.getByRole('list', { name: 'Tags' })).getAllByRole('listitem')
    const legacy = items.find((li) => li.textContent?.includes('legacy'))
    const env = items.find((li) => li.textContent?.startsWith('env'))
    expect(legacy).toHaveTextContent('(not sent to the model)')
    expect(env).not.toHaveTextContent('(not sent to the model)')
    // The summary line is wired as the list's description, so a reader jumping to the list still gets it.
    const hintId = screen.getByRole('list', { name: 'Tags' }).getAttribute('aria-describedby')
    expect(document.getElementById(hintId as string)).toHaveTextContent(/kept but not sent/i)
  })

  it('marks a message tag the evaluation no longer allows', async () => {
    // The page computes the unsent set per message and hands it to each bubble. Without that wiring the
    // component-level test still passes while every message chip claims the model received context it
    // never did — and unlike a conversation tag, a message tag cannot be corrected afterwards.
    server.use(...baseHandlers(conversationTombstonedScenario))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([
          { id: 'tk-1', evaluation_id: EVAL_ID, key: 'env', created_at: '2026-01-01T00:00:00Z' },
        ]),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
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
                tags: { env: 'prod', legacy: 'x' },
              },
            ],
            total: 1,
            limit: 100,
            offset: 0,
          }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    const items = within(await screen.findByTestId('message-tags')).getAllByRole('listitem')
    const legacy = items.find((li) => li.textContent?.includes('legacy'))
    const env = items.find((li) => li.textContent?.startsWith('env'))
    expect(legacy).toHaveTextContent('(not sent to the model)')
    expect(env).not.toHaveTextContent('(not sent to the model)')
  })

  it('clears the not-sent marker for a tag the paired reply actually recorded', async () => {
    // The reply's own `tag_context` is proof of what was sent, so it overrides today's policy for
    // any key it carries — `legacy`, absent from the record, still gets today's policy verdict.
    server.use(...baseHandlers(conversationTombstonedScenario))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([]),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
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
                tags: { env: 'prod', legacy: 'x' },
              },
              {
                id: 'msg-assistant-000-000000000000',
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'reply',
                status: 'complete',
                created_at: '2026-01-01T00:00:02Z',
                tag_context: { env: 'prod' },
              },
            ],
            total: 2,
            limit: 100,
            offset: 0,
          }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    const items = within(await screen.findByTestId('message-tags')).getAllByRole('listitem')
    const env = items.find((li) => li.textContent?.startsWith('env'))
    const legacy = items.find((li) => li.textContent?.includes('legacy'))
    expect(env).not.toHaveTextContent('(not sent to the model)')
    expect(legacy).toHaveTextContent('(not sent to the model)')
  })

  it('marks a policy-allowed tag the paired reply did not record', async () => {
    // The other direction of the same proof: a non-empty record decides both ways, so a key it
    // omits carries the marker even though today's policy would allow it. Without this the chip
    // claims the model received a tag the record does not confirm.
    server.use(...baseHandlers(conversationTombstonedScenario))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([{ id: 'key-1', evaluation_id: EVAL_ID, key: 'goal' }]),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
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
                id: 'msg-assistant-000-000000000000',
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
    renderWithPerms(['conversations:read', 'conversations:update'])

    const items = within(await screen.findByTestId('message-tags')).getAllByRole('listitem')
    const goal = items.find((li) => li.textContent?.startsWith('goal'))
    const persona = items.find((li) => li.textContent?.startsWith('persona'))
    expect(goal).toHaveTextContent('(not sent to the model)')
    expect(persona).not.toHaveTextContent('(not sent to the model)')
  })

  it('renders the recorded tag context on an assistant reply', async () => {
    // Guards the page dropping the `tagContext={m.tag_context}` wire into `Bubble`.
    server.use(...baseHandlers(conversationTombstonedScenario))
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-assistant-000-000000000000',
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'here you go',
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
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    // Per-message id: a transcript with two records has two addressable blocks.
    expect(
      await screen.findByTestId('tag-context-msg-assistant-000-000000000000'),
    ).toHaveTextContent('env: prod')
  })

  it('names a stored tag that has no value', async () => {
    // `render_tag_context` skips an unfilled value, so this tag is stored and chipped but never sent.
    server.use(...baseHandlers({ ...conversationTombstonedScenario, tags: { persona: '' } }))
    renderWithPerms(['conversations:read', 'conversations:update'])

    expect(await screen.findByText(/kept but not sent to the model: persona/i)).toBeInTheDocument()
    const chip = within(screen.getByRole('list', { name: 'Tags' })).getByRole('listitem')
    expect(chip).toHaveTextContent('(not sent to the model)')
  })

  it('claims nothing about the tags until the evaluation itself has resolved', async () => {
    // `tagsEnabled` is `false` while that query is unsettled — and permanently if it fails — so a page
    // that marked chips from it would say "the model never got this" about an evaluation it never read.
    server.use(...baseHandlers({ ...conversationTombstonedScenario, tags: { env: 'prod' } }))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    const row = await screen.findByTestId('conversation-tags')
    expect(row).toHaveTextContent('env: prod')
    expect(row).not.toHaveTextContent('(not sent to the model)')
    expect(screen.queryByText(/tagging is off for this evaluation/i)).toBeNull()
    expect(screen.queryByText(/kept but not sent/i)).toBeNull()
  })

  it('marks no tag unsent when the allow-list read failed', async () => {
    // An errored list is `[]`, which would otherwise read as "nothing is allowed" and brand every
    // stored tag as filtered out on an evaluation that is perfectly healthy. The chips render before
    // the query settles, so the dialog's error copy is what proves the failed state was reached —
    // asserting straight after the row only ever observed `pending`.
    const user = userEvent.setup()
    server.use(...baseHandlers({ ...conversationTombstonedScenario, tags: { env: 'prod' } }))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    await user.click(await screen.findByRole('button', { name: /edit tags/i }))
    expect(await screen.findByText(/couldn.t load the allowed keys/i)).toBeInTheDocument()

    expect(screen.getByTestId('conversation-tags')).toBeInTheDocument()
    expect(screen.queryByText(/not sent to the model/i)).toBeNull()
  })

  it('advertises the allowed keys in both authoring surfaces when the evaluation restricts tags', async () => {
    server.use(...baseHandlers(conversationWithTags))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json([
          {
            id: 'tk-1',
            evaluation_id: EVAL_ID,
            key: 'env',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
          {
            id: 'tk-2',
            evaluation_id: EVAL_ID,
            key: 'owner',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ]),
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    // Per-message tags go through the same gate as the conversation's, so the composer restricts too.
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    // The list is a portalled popover, so it has to be opened before its items exist.
    await user.click(screen.getByRole('combobox', { name: 'Message tag 1 key' }))
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      '— select key —',
      'env',
      'owner',
    ])
    await user.keyboard('{Escape}')

    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    // The conversation already carries `env`, so only the remaining allowed key is still on offer.
    const dialogKey = within(screen.getByRole('dialog')).getByRole('combobox', {
      name: 'Tag 1 key',
    })
    expect(dialogKey).toHaveTextContent('env')
    await user.click(dialogKey)
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      '— select key —',
      'env',
      'owner',
    ])
  })

  it('does not claim an empty allow-list while the keys query has not settled', async () => {
    // A failed query used to read as "restricted with zero allowed keys", which labels every existing
    // key stale and invites the operator to delete a valid tag. On an error that state is permanent.
    server.use(...baseHandlers(conversationWithTags))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({ ...evaluation, tags_restricted: true }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit tags/i }))
    const dialog = within(screen.getByRole('dialog'))

    expect(dialog.queryByText(/No keys are allowed yet/i)).toBeNull()
    expect(dialog.queryByText(/no longer allowed/i)).toBeNull()
  })

  it('does not fetch the allowed keys while the evaluation leaves tags free-form', async () => {
    let tagKeyCalls = 0
    server.use(...baseHandlers(conversationWithTags))
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () => {
        tagKeyCalls += 1
        return HttpResponse.json([])
      }),
    )
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    expect(tagKeyCalls).toBe(0)
  })

  it('shows the real reason inline when the failure is not about the tags', async () => {
    server.use(
      ...baseHandlers(conversationTombstonedScenario),
      http.patch(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
        HttpResponse.json({ detail: 'Conversation is finished.' }, { status: 409 }),
      ),
    )
    const user = userEvent.setup()
    renderWithPerms(['conversations:read', 'conversations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Single conversation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /add tags/i }))
    await user.type(await screen.findByLabelText('Tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    // The mutation opts out of the global toast, so this dialog is the only channel: it has to render
    // the reason itself. Asserting the absence of a string instead would be vacuous — the old
    // fallback copy no longer exists anywhere in the source, so `queryByText` could never fail.
    // No tags on this fixture, so the dialog titles itself "Add tags".
    await waitFor(() =>
      expect(screen.getByRole('dialog', { name: 'Add tags' })).toBeInTheDocument(),
    )
    expect(await screen.findByText('Conversation is finished.')).toBeInTheDocument()
  })

  it('does not offer tag editing without conversations:update', async () => {
    server.use(...baseHandlers(conversationWithTags))
    renderWithPerms(['conversations:read'])

    // Chips still render (read), but the edit affordance is gated on the write permission.
    // (Scope to the conversation-level button — the composer has its own "Tags" toggle.)
    await waitFor(() => expect(screen.getByText('env: prod')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /(add|edit) tags/i })).toBeNull()
  })
})

describe('ConversationDetailPage — composer warmup gate', () => {
  it('holds sending while the evaluation (hence the warmup gate) is still loading', async () => {
    // The composer renders off the conversation query, but the warmup flag lives on
    // the evaluation query. If the composer went live before the evaluation resolved,
    // a deep-link could fire the first message into a still-cold endpoint.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}`, () =>
        HttpResponse.json(conversationTombstonedScenario),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () => HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/${TOMBSTONED_SCENARIO_ID}`,
        () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 }),
      ),
      http.get('http://localhost/api/v1/message-flags', () =>
        HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
      ),
      groupHandler(),
      // Evaluation never resolves → the assignment (and its warmup flag) stays unknown.
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => new Promise(() => {})),
    )
    const user = userEvent.setup()
    renderPage()

    // Typing removes the empty-input gate, so a still-disabled Send proves the warmup hold.
    await user.type(await screen.findByLabelText('Message'), 'hello')
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })
})

describe('ConversationDetailPage — the way back to the evaluation group', () => {
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
      ...baseHandlers(conversationTombstonedScenario),
    )
    renderPage()

    const identity = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(
      await within(identity).findByRole('link', { name: 'Spring Jailbreak Sprint' }),
    ).toHaveAttribute('href', '/evaluation-groups/grp-0000-0000-0000-000000000000')

    // The path opens with the group on every page in the subtree, not only on the evaluation ones.
    const path = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(path).getByRole('link', { name: 'Spring Jailbreak Sprint' })).toBeInTheDocument()
  })
})

describe('ConversationDetailPage — in-group authority', () => {
  it('renders the transcript for a group-scoped red teamer with no global conversation permission', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:participate',
      ]),
      ...baseHandlers(conversationTombstonedScenario),
    )
    renderWithPerms(['evaluations:read'])

    expect(await screen.findByRole('heading', { name: 'Single conversation' })).toBeInTheDocument()
  })

  it('refuses when neither the JWT nor the in-group role grants read', async () => {
    server.use(...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['evaluations:read'])

    expect(await screen.findByText(/access to this section/i)).toBeInTheDocument()
  })
})

describe('ConversationDetailPage — a member transcript surfaced by read_any', () => {
  const foreign: ConversationResponse = {
    ...conversationTombstonedScenario,
    user_id: 'someone-else-0000-0000-000000000000',
  }

  it('shows the transcript but no write affordances on another member conversation', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, [
        'conversations:read',
        'conversations:read_any',
      ]),
      // An assistant message on purpose: the flag checkbox only renders on one, so asserting its
      // absence against an empty transcript would pass whatever the gate did.
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-0001-0000-0000-000000000000',
                conversation_id: CONV_ID,
                role: 'assistant',
                content: 'a member reply',
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
      ...baseHandlers(foreign),
    )
    // Global write permissions on purpose: `read_any` widens reads only, so the server would
    // refuse every one of these writes even though the caller carries the permission.
    renderWithPerms(['conversations:read', 'conversations:update', 'conversations:delete'])

    expect(await screen.findByRole('heading', { name: 'Single conversation' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^rename$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
    expect(screen.queryByLabelText('Message')).toBeNull()
    // Anchored on the model line, which renders only once the evaluation (and so the assignment
    // the probe needs) has resolved — otherwise a zero count could just mean "not yet".
    expect(await screen.findByText(/Model: gpt-4o/)).toBeInTheDocument()
    // Warming is a write the caller cannot make: probing anyway reports the 403 as the model
    // being unavailable, next to a transcript that loaded fine.
    expect(warmupCalls).toBe(0)
    // The badge is the visible half — with the gate reverted this reads "Model ready".
    expect(screen.queryByText(/model ready|waking|unavailable/i)).toBeNull()
    // Flag authoring is owner-only server-side even for break-glass, so the whole flow would 404.
    expect(await screen.findByText('a member reply')).toBeInTheDocument()
    expect(screen.queryByLabelText('Select message to flag')).toBeNull()
  })

  it('surfaces a failed authority read as an error with a way back, not as a refusal', async () => {
    // `failed` is the third state of `useGroupAuthority`: a group that will not load is not a
    // permission verdict, so it must not render as "no access".
    server.use(
      // First match wins, so the failing group read has to precede the generic handler.
      http.get(`http://localhost/api/v1/evaluation-groups/${evaluation.evaluation_group_id}`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
      ...baseHandlers(foreign),
    )
    renderWithPerms(['evaluations:read'])

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /back to evaluation/i })).toBeInTheDocument()
    expect(screen.queryByText(/access to this section/i)).toBeNull()
  })

  it('keeps the flag surface for the conversation owner', async () => {
    // The negative case above only proves absence; without this, narrowing the union that feeds
    // `canFlag` would remove flagging for every owner with the suite green.
    server.use(
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: 'msg-0002-0000-0000-000000000000',
                conversation_id: CONV_ID,
                role: 'assistant',
                content: 'my own reply',
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
      ...baseHandlers(conversationTombstonedScenario),
    )
    renderWithPerms(['conversations:read', 'flags:create'])

    expect(await screen.findByText('my own reply')).toBeInTheDocument()
    expect(screen.getByLabelText('Select message to flag')).toBeInTheDocument()
  })

  it('keeps the write affordances for a break-glass admin', async () => {
    server.use(
      parentGroupHandler(evaluation.evaluation_group_id, ['conversations:read']),
      ...baseHandlers(foreign),
    )
    renderWithPerms([
      'conversations:read',
      'conversations:update',
      'conversations:delete',
      'evaluation_groups:manage',
    ])

    expect(await screen.findByRole('heading', { name: 'Single conversation' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^rename$/i })).toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toBeInTheDocument()
  })
})

describe('ConversationDetailPage — message annotations', () => {
  const LABEL = { id: 'label-jb', key: 'jailbreak', name: 'Jailbreak' }
  const ANNOTATED_MSG = 'msg-assistant-000-000000000000'

  function annotationHandlers(annotations: unknown[] = []) {
    return [
      http.get('http://localhost/api/v1/annotations', () =>
        HttpResponse.json({
          items: annotations,
          total: annotations.length,
          limit: 100,
          offset: 0,
        }),
      ),
      http.get('http://localhost/api/v1/annotation-labels', () =>
        HttpResponse.json({ items: [LABEL], total: 1, limit: 100, offset: 0 }),
      ),
      http.get(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/${CONV_ID}/messages`,
        () =>
          HttpResponse.json({
            items: [
              {
                id: ANNOTATED_MSG,
                turn_id: 'turn-1',
                role: 'assistant',
                content: 'here you go',
                status: 'complete',
                created_at: '2026-01-01T00:00:02Z',
              },
            ],
            total: 1,
            limit: 100,
            offset: 0,
          }),
      ),
    ]
  }

  const annotation = {
    id: 'ann-1',
    message_id: ANNOTATED_MSG,
    conversation_id: CONV_ID,
    label: LABEL,
    text: null,
    created_by_id: 'user-0002',
    evaluation_id: EVAL_ID,
    evaluation_group_id: 'group-1',
    created_at: '2026-08-27T12:00:00Z',
    updated_at: '2026-08-27T12:00:00Z',
  }

  it('renders another author’s labels on the transcript', async () => {
    // Reads are shared server-side, so the conversation owner sees an annotator's labels here.
    server.use(...annotationHandlers([annotation]), ...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read', 'annotations:read'])

    expect(await screen.findByTestId('message-annotations')).toBeInTheDocument()
    expect(screen.getByText('Jailbreak')).toBeInTheDocument()
  })

  it('gates the affordance on the global annotations:create key, not on ownership', async () => {
    // The annotations routes read the JWT claim, which carries global roles only — so this gate
    // deliberately does not use the in-group union the flag gate above it uses. A refactor back
    // to `allows(...)` would offer the button to an in-group annotator the server then 403s.
    server.use(...annotationHandlers(), ...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read', 'annotations:read'])

    await waitFor(() => expect(screen.getByText('here you go')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /label this message/i })).not.toBeInTheDocument()
  })

  it('shows it once the caller holds annotations:create globally', async () => {
    server.use(...annotationHandlers(), ...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read', 'annotations:read', 'annotations:create'])

    expect(await screen.findByRole('button', { name: /label this message/i })).toBeInTheDocument()
  })

  it('says so when the conversation holds more annotations than one page', async () => {
    // One page, newest-first: past the cap the oldest-annotated messages render no chips, which
    // reads as unlabelled — the misread the failed-read surface above exists to prevent.
    server.use(
      http.get('http://localhost/api/v1/annotations', () =>
        HttpResponse.json({ items: [annotation], total: 250, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(conversationTombstonedScenario),
    )
    renderWithPerms(['conversations:read', 'annotations:read'])

    expect(
      await screen.findByText(/showing the 100 most recent labels of 250/i),
    ).toBeInTheDocument()
  })

  it('stays silent when the whole set fits in one page', async () => {
    // The negative half: without it an always-on note (a flipped comparison) keeps the suite green
    // while telling every operator their transcript is incomplete.
    server.use(...annotationHandlers([annotation]), ...baseHandlers(conversationTombstonedScenario))
    renderWithPerms(['conversations:read', 'annotations:read'])

    expect(await screen.findByTestId('message-annotations')).toBeInTheDocument()
    expect(screen.queryByText(/most recent labels of/i)).not.toBeInTheDocument()
  })
})
