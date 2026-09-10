import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ConversationGroupsListPage } from './conversation-groups-list-page'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const GROUP_ID = 'grp-0001-0000-0000-000000000000'

function member(id: string) {
  return {
    id,
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
            evaluation_group_id: 'g1',
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['conversations:read', 'evaluations:read'])
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
}

describe('ConversationGroupsListPage', () => {
  it('lists groups with the resolved evaluation name and member count', async () => {
    server.use(...handlers([groupRow]))
    renderPage()

    await waitFor(() => expect(screen.getByText('GPT-4o vs Claude')).toBeInTheDocument())
    expect(screen.getByText('Jailbreak suite')).toBeInTheDocument()
    expect(screen.getByText('2 models')).toBeInTheDocument()
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
})
