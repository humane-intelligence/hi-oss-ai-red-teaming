import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { AUTH_USER_ID, authWrapper } from '@/lib/auth/auth.testutils'
import { ConversationsSection } from './conversations-section'
import type { EvaluationResponse, ConversationResponse } from '@/lib/api/types'
import { licenseStub } from '@/features/licenses/test-fixtures'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const MODEL_ID = 'model-001-0000-0000-000000000000'
const CONV_ID = 'conv-0001-0000-0000-000000000000'

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
      warmup_enabled: false,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
  ],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const MODEL_B = 'model-002-0000-0000-000000000000'
const CONV_B = 'conv-0002-0000-0000-000000000000'
const CONV_C = 'conv-0003-0000-0000-000000000000'
const GROUP_MULTI = 'grp-multi-0000-0000-000000000000'
const GROUP_SINGLE = 'grp-single-000-0000-000000000000'

const evaluation2: EvaluationResponse = {
  ...evaluation,
  models: [
    {
      assignment_id: MODEL_ID,
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
}

function conv(id: string, modelId: string, groupId: string): ConversationResponse {
  return {
    id,
    user_id: AUTH_USER_ID,
    evaluation_id: EVAL_ID,
    evaluation_ai_model_id: modelId,
    content_protected: false,
    scenario_id: 'scn-0001-0000-0000-000000000000',
    conversation_group_id: groupId,
    effective_license: licenseStub('CC-BY-4.0'),
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

const multiGroup = {
  id: GROUP_MULTI,
  user_id: AUTH_USER_ID,
  evaluation_id: EVAL_ID,
  name: 'GPT-4o vs Claude',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  conversations: [conv(CONV_ID, MODEL_ID, GROUP_MULTI), conv(CONV_B, MODEL_B, GROUP_MULTI)],
}

const singleGroup = {
  id: GROUP_SINGLE,
  user_id: AUTH_USER_ID,
  evaluation_id: EVAL_ID,
  name: 'Quick probe',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  conversations: [conv(CONV_C, MODEL_ID, GROUP_SINGLE)],
}

function groupsHandler(groups: unknown[]) {
  return http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups`, () =>
    HttpResponse.json({ items: groups, total: groups.length, limit: 100, offset: 0 }),
  )
}

function deletedConversationsHandler(items: ConversationResponse[]) {
  return http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations`, ({ request }) => {
    // Mirrors the server: this path serves live rows too, and only `deleted=true` returns
    // tombstones — so a fixture that answered both would be lying.
    const deleted = new URL(request.url).searchParams.get('deleted') === 'true'
    const rows = deleted ? items : []
    return HttpResponse.json({ items: rows, total: rows.length, limit: 100, offset: 0 })
  })
}

function tombstone(id: string, deletedAt: string): ConversationResponse {
  return { ...conv(id, MODEL_ID, GROUP_SINGLE), title: null, deleted_at: deletedAt }
}

function renderSection(
  perms = [
    'conversations:read',
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
        <MemoryRouter initialEntries={['/start']}>
          <Routes>
            <Route path="/start" element={<ConversationsSection evaluation={evaluation2} />} />
            <Route
              path="/evaluations/:id/conversations/:conversationId"
              element={<div>CONVERSATION DETAIL</div>}
            />
            <Route
              path="/evaluations/:id/conversation-groups/:groupId"
              element={<div>GROUP PAGE</div>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ConversationsSection — groups', () => {
  it('renders each group with its name and member model names', async () => {
    server.use(groupsHandler([multiGroup, singleGroup]))
    renderSection()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    expect(screen.getByText('Quick probe')).toBeInTheDocument()
    // counter = number of sessions (2 groups), not the 3 threads they contain
    expect(screen.getByText('Conversations (2)')).toBeInTheDocument()
    // gpt-4o is a member of both groups; claude-3-opus only of the multi group.
    expect(screen.getAllByText('gpt-4o')).toHaveLength(2)
    expect(screen.getByText('claude-3-opus')).toBeInTheDocument()
  })

  it('shows a member title in place of the model name, with the model as a subtitle', async () => {
    const titledGroup = {
      ...singleGroup,
      conversations: [{ ...conv(CONV_C, MODEL_ID, GROUP_SINGLE), title: 'Direct ask' }],
    }
    server.use(groupsHandler([titledGroup]))
    renderSection()

    await waitFor(() => expect(screen.getByText('Direct ask')).toBeInTheDocument())
    expect(screen.getByText(/gpt-4o/)).toBeInTheDocument()
  })

  it('opens the side-by-side group page for a multi-member group', async () => {
    const user = userEvent.setup()
    server.use(groupsHandler([multiGroup]))
    renderSection()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    await user.click(screen.getByRole('link', { name: /open side-by-side/i }))
    await waitFor(() => expect(screen.getByText('GROUP PAGE')).toBeInTheDocument())
  })

  it('opens the conversation detail when a member is clicked', async () => {
    const user = userEvent.setup()
    server.use(groupsHandler([singleGroup]))
    renderSection()

    await waitFor(() => expect(screen.getByText('Quick probe')).toBeInTheDocument())
    await user.click(screen.getByText('gpt-4o'))
    await waitFor(() => expect(screen.getByText('CONVERSATION DETAIL')).toBeInTheDocument())
  })

  it('renames a group', async () => {
    let captured: unknown = null
    server.use(
      groupsHandler([multiGroup]),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_MULTI}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...multiGroup, name: 'Renamed' })
        },
      ),
    )
    const user = userEvent.setup()
    renderSection()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /rename conversation/i }))
    const input = await screen.findByLabelText('Name')
    await user.clear(input)
    await user.type(input, 'Renamed')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { name: string }).name).toBe('Renamed')
  })

  it('deletes a group after confirmation', async () => {
    let deleted = false
    server.use(
      groupsHandler([multiGroup]),
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_MULTI}`,
        () => {
          deleted = true
          return new HttpResponse(null, { status: 204 })
        },
      ),
    )
    const user = userEvent.setup()
    renderSection()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /delete conversation/i }))
    await user.click(screen.getByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(deleted).toBe(true))
  })

  it('shows the empty state when there are no groups', async () => {
    server.use(groupsHandler([]))
    renderSection()

    await waitFor(() => expect(screen.getByText(/no conversations yet/i)).toBeInTheDocument())
  })
})

describe('ConversationsSection — recently deleted', () => {
  it('fetches nothing until the section is opened, then lists and restores', async () => {
    const user = userEvent.setup()
    const deletedCalls: string[] = []
    const restored: string[] = []
    server.use(
      groupsHandler([singleGroup]),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations`, ({ request }) => {
        deletedCalls.push(request.url)
        const items = [tombstone(CONV_B, '2026-01-02T10:00:00Z')]
        return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
      }),
      http.post(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversations/:conversationId/restore`,
        ({ params }) => {
          restored.push(params.conversationId as string)
          return HttpResponse.json(conv(CONV_B, MODEL_ID, GROUP_SINGLE))
        },
      ),
    )
    renderSection()

    // Collapsed: the header is there, the request is not.
    const toggle = await screen.findByRole('button', { name: /recently deleted/i })
    await waitFor(() => expect(screen.getByText('Quick probe')).toBeInTheDocument())
    expect(deletedCalls).toEqual([])
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await user.click(toggle)

    await waitFor(() => expect(deletedCalls.length).toBe(1))
    expect(deletedCalls[0]).toContain('deleted=true')
    await user.click(screen.getByRole('button', { name: /restore/i }))

    await waitFor(() => expect(restored).toEqual([CONV_B]))
  })

  it('says so when nothing was deleted', async () => {
    const user = userEvent.setup()
    server.use(groupsHandler([singleGroup]), deletedConversationsHandler([]))
    renderSection()

    await user.click(await screen.findByRole('button', { name: /recently deleted/i }))

    expect(await screen.findByText(/nothing deleted recently/i)).toBeInTheDocument()
  })

  it('offers no section at all without conversations:delete', async () => {
    server.use(
      groupsHandler([singleGroup]),
      deletedConversationsHandler([tombstone(CONV_B, '2026-01-02T10:00:00Z')]),
    )
    renderSection(['conversations:read', 'conversations:create', 'conversations:update'])

    await waitFor(() => expect(screen.getByText('Quick probe')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /recently deleted/i })).toBeNull()
  })

  it('refreshes the deleted list after a group delete cascades into it', async () => {
    // Report from manual testing: deleting a conversation on the evaluation page left the
    // Recently deleted list stale, because the group delete never invalidated it.
    const user = userEvent.setup()
    let deleted: ConversationResponse[] = []
    server.use(
      groupsHandler([multiGroup]),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversations`, () =>
        HttpResponse.json({ items: deleted, total: deleted.length, limit: 100, offset: 0 }),
      ),
      http.delete(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups/${GROUP_MULTI}`,
        () => {
          // The server cascades the group's members; mirror that in the fixture.
          deleted = [tombstone(CONV_ID, '2026-01-02T10:00:00Z')]
          return new HttpResponse(null, { status: 204 })
        },
      ),
    )
    renderSection()

    await user.click(await screen.findByRole('button', { name: /recently deleted/i }))
    expect(await screen.findByText(/nothing deleted recently/i)).toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: /delete conversation/i })[0]!)
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }))

    // No reload, no re-open: the open section picks the tombstone up on its own.
    expect(await screen.findByText('Recently deleted (1)')).toBeInTheDocument()
  })
})

describe('ConversationsSection — groups surfaced by read_any', () => {
  it("marks another member's group and offers no rename or delete on it", async () => {
    server.use(groupsHandler([{ ...multiGroup, user_id: 'someone-else-0000-0000-000000000000' }]))
    renderSection()

    expect(await screen.findByText("Another member's")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /rename conversation/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete conversation/i })).toBeNull()
  })
})
