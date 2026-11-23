import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { toast } from 'sonner'
import { AUTH_USER_ID, authWrapper } from '@/lib/auth/auth.testutils'
import { ConversationGroupsListPage } from './conversation-groups-list-page'
import { parentGroupHandler } from './test-fixtures'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const PARENT_GROUP_ID = 'g1'
const GROUP_ID = 'grp-0001-0000-0000-000000000000'

function member(id: string, turn_count = 2) {
  return {
    id,
    turn_count,
    user_id: 'u1',
    evaluation_id: EVAL_ID,
    evaluation_ai_model_id: 'm1',
    conversation_group_id: GROUP_ID,
    created_at: '2026-01-02T10:00:00Z',
    updated_at: '2026-01-02T10:00:00Z',
  }
}

const groupRow = {
  id: GROUP_ID,
  user_id: 'u1',
  evaluation_id: EVAL_ID,
  name: 'GPT-4o vs Claude',
  created_at: '2026-01-02T10:00:00Z',
  updated_at: '2026-01-02T10:00:00Z',
  conversations: [member('c1'), member('c2')],
}

function handlers(groups: unknown[] = []) {
  return [
    http.get('http://localhost/api/v1/conversation-groups', () =>
      HttpResponse.json({ items: groups, total: groups.length, limit: 20, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/evaluations', () =>
      HttpResponse.json({
        items: [
          {
            id: EVAL_ID,
            title: 'Jailbreak suite',
            status: 'new',
            evaluation_group_id: PARENT_GROUP_ID,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 100,
        offset: 0,
      }),
    ),
  ]
}

const ownRow = { ...groupRow, user_id: AUTH_USER_ID }
const DELETE_NAME = 'Delete GPT-4o vs Claude'

// Records the evaluation reads that only the in-group permission check makes.
function authorityHandlers(groupPermissions: string[]) {
  const reads: string[] = []
  server.use(
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, ({ request }) => {
      reads.push(request.url)
      return HttpResponse.json({ id: EVAL_ID, evaluation_group_id: PARENT_GROUP_ID, models: [] })
    }),
    parentGroupHandler(PARENT_GROUP_ID, groupPermissions),
  )
  return reads
}

// No <Toaster> is mounted in tests, so the Undo handler comes off the mocked toast call.
function undoFromLastToast(): () => void {
  const calls = vi.mocked(toast.success).mock.calls
  const last = calls[calls.length - 1] as unknown as [string, { action: { onClick: () => void } }]
  return last[1].action.onClick
}

function renderPage(extraPermissions: string[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['conversations:read', 'evaluations:read', ...extraPermissions])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/conversation-groups']}>
          <Routes>
            <Route path="/conversation-groups" element={<ConversationGroupsListPage />} />
            <Route
              path="/evaluations/:id/conversation-groups/:groupId"
              element={<div>GROUP PAGE</div>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
  return qc
}

describe('ConversationGroupsListPage', () => {
  it('lists groups with the resolved evaluation name and member count', async () => {
    server.use(...handlers([groupRow]))
    renderPage()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    expect(screen.getByText('Jailbreak suite')).toBeInTheDocument()
    expect(screen.getByText('2 models')).toBeInTheDocument()
  })

  it('shows the short conversation ID, with the full ID on hover', async () => {
    server.use(...handlers([groupRow]))
    renderPage()

    const id = await screen.findByText('grp-0001')
    expect(id).toHaveAttribute('title', GROUP_ID)
  })

  it('counts prompts as the most turns any of the group’s models has', async () => {
    server.use(...handlers([{ ...groupRow, conversations: [member('c1', 3), member('c2', 5)] }]))
    renderPage()

    const row = (await screen.findByText('GPT-4o vs Claude')).closest('tr')
    if (!row) throw new Error('row not found')
    const header = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(within(row).getAllByRole('cell')[header.indexOf('Prompts')]).toHaveTextContent(/^5$/)
  })

  it('marks a group as empty only when none of its members has a turn', async () => {
    server.use(
      ...handlers([
        {
          ...groupRow,
          id: 'grp-empty',
          name: 'Abandoned',
          conversations: [member('c3', 0), member('c4', 0)],
        },
        {
          ...groupRow,
          id: 'grp-half',
          name: 'Half used',
          conversations: [member('c5', 0), member('c6', 1)],
        },
      ]),
    )
    renderPage()

    const abandoned = await screen.findByRole('row', { name: /abandoned/i })
    expect(within(abandoned).getByText('No messages')).toBeInTheDocument()
    const halfUsed = screen.getByRole('row', { name: /half used/i })
    expect(within(halfUsed).queryByText('No messages')).toBeNull()
  })

  it('marks a group whose conversations were all deleted as empty', async () => {
    server.use(...handlers([{ ...groupRow, name: 'Emptied', conversations: [] }]))
    renderPage()

    const emptied = await screen.findByRole('row', { name: /emptied/i })
    expect(within(emptied).getByText('No messages')).toBeInTheDocument()
  })

  it("marks a row that is another member's", async () => {
    server.use(...handlers([{ ...groupRow, user_id: 'someone-else' }]))
    renderPage()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    expect(screen.getByText("Another member's")).toBeInTheDocument()
  })

  it('offers only the data columns in the Columns menu', async () => {
    const user = userEvent.setup()
    server.use(...handlers([ownRow]))
    renderPage(['conversations:delete'])

    await screen.findByRole('button', { name: DELETE_NAME })
    await user.click(screen.getByRole('button', { name: 'Toggle columns' }))
    expect(screen.getAllByRole('checkbox').map((c) => c.closest('label')?.textContent)).toEqual([
      'Name',
      'ID',
      'Evaluation',
      'Models',
      'Prompts',
      'Started',
    ])
  })

  it('opens the group page on row click', async () => {
    const user = userEvent.setup()
    server.use(...handlers([groupRow]))
    renderPage()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    await user.click(screen.getByText('GPT-4o vs Claude'))
    await waitFor(() => expect(screen.getByText('GROUP PAGE')).toBeInTheDocument())
  })

  it('shows the empty state when there are no groups', async () => {
    server.use(...handlers([]))
    renderPage()

    await waitFor(() => expect(screen.getByText(/no conversations yet/i)).toBeInTheDocument())
  })

  describe('delete', () => {
    beforeEach(() => vi.mocked(toast.success).mockClear())

    it("deletes against the group's own evaluation without opening it, and Undo restores it", async () => {
      const groups: unknown[] = [ownRow]
      const deleted: string[] = []
      const restored: string[] = []
      server.use(
        ...handlers(groups),
        http.delete(
          'http://localhost/api/v1/evaluations/:evaluationId/conversation-groups/:groupId',
          ({ params }) => {
            deleted.push(`${params.evaluationId}/${params.groupId}`)
            groups.length = 0
            return new HttpResponse(null, { status: 204 })
          },
        ),
        http.post(
          'http://localhost/api/v1/evaluations/:evaluationId/conversations/:conversationId/restore',
          ({ params }) => {
            restored.push(`${params.evaluationId}/${params.conversationId}`)
            return HttpResponse.json({ id: params.conversationId, deleted_at: null })
          },
        ),
      )
      const user = userEvent.setup()
      renderPage(['conversations:delete'])

      await user.click(await screen.findByRole('button', { name: DELETE_NAME }))
      await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))

      await waitFor(() => expect(deleted).toEqual([`${EVAL_ID}/${GROUP_ID}`]))
      await waitFor(() => expect(screen.getByText(/no conversations yet/i)).toBeInTheDocument())
      expect(screen.queryByText('GROUP PAGE')).toBeNull()

      await act(async () => undoFromLastToast()())
      await waitFor(() => expect(restored).toEqual([`${EVAL_ID}/c1`, `${EVAL_ID}/c2`]))
    })

    it('steps back a page when the last row of the last page is deleted', async () => {
      // 20 rows of another member fill page 1; the caller's own row is alone on page 2.
      const groups: (typeof groupRow)[] = [
        ...Array.from({ length: 20 }, (_, i) => ({
          ...groupRow,
          id: `g-${i}`,
          name: `Other ${i}`,
        })),
        ownRow,
      ]
      server.use(
        http.get('http://localhost/api/v1/conversation-groups', ({ request }) => {
          const offset = Number(new URL(request.url).searchParams.get('offset') ?? 0)
          return HttpResponse.json({
            items: groups.slice(offset, offset + 20),
            total: groups.length,
            limit: 20,
            offset,
          })
        }),
        ...handlers(),
        http.delete(
          'http://localhost/api/v1/evaluations/:evaluationId/conversation-groups/:groupId',
          () => {
            groups.pop()
            return new HttpResponse(null, { status: 204 })
          },
        ),
      )
      const user = userEvent.setup()
      renderPage(['conversations:delete'])

      await screen.findByText('1–20 of 21')
      await user.click(screen.getByRole('button', { name: 'Next' }))
      await user.click(await screen.findByRole('button', { name: DELETE_NAME }))
      await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))

      expect(await screen.findByText('1–20 of 20')).toBeInTheDocument()
      expect(await screen.findByText('Other 0')).toBeInTheDocument()
      expect(screen.queryByText(/no conversations yet/i)).toBeNull()
    })

    it("hides it on another member's group", async () => {
      server.use(...handlers([groupRow]))
      renderPage(['conversations:delete'])

      await screen.findByText('GPT-4o vs Claude')
      expect(screen.queryByRole('button', { name: DELETE_NAME })).toBeNull()
    })

    it("shows it on another member's group to a manager", async () => {
      server.use(...handlers([groupRow]))
      renderPage(['conversations:delete', 'evaluation_groups:manage'])

      expect(await screen.findByRole('button', { name: DELETE_NAME })).toBeInTheDocument()
    })

    it('shows it on an own group from the JWT without reading the parent group', async () => {
      server.use(...handlers([ownRow]))
      const reads = authorityHandlers([])
      renderPage(['conversations:delete'])

      expect(await screen.findByRole('button', { name: DELETE_NAME })).toBeInTheDocument()
      expect(reads).toEqual([])
    })

    it("shows it on an own group from the parent group's role", async () => {
      server.use(...handlers([ownRow]))
      authorityHandlers(['conversations:delete'])
      renderPage()

      expect(await screen.findByRole('button', { name: DELETE_NAME })).toBeInTheDocument()
    })

    it('keeps the per-row permission reads out of the global error toast', async () => {
      server.use(...handlers([ownRow]))
      authorityHandlers(['conversations:delete'])
      const qc = renderPage()

      await screen.findByRole('button', { name: DELETE_NAME })
      // The global `QueryCache.onError` reads this meta to skip its toast.
      for (const key of [
        ['evaluation', EVAL_ID],
        ['evaluation-group', PARENT_GROUP_ID],
      ])
        expect(qc.getQueryCache().find({ queryKey: key })?.meta).toEqual({
          suppressErrorToast: true,
        })
    })

    it('hides it on an own group when neither grants delete', async () => {
      server.use(...handlers([ownRow]))
      authorityHandlers([])
      const qc = renderPage()

      await waitFor(() =>
        expect(qc.getQueryState(['evaluation-group', PARENT_GROUP_ID])?.status).toBe('success'),
      )
      await expect(
        screen.findByRole('button', { name: DELETE_NAME }, { timeout: 200 }),
      ).rejects.toThrow(/Unable to find/)
    })
  })
})
