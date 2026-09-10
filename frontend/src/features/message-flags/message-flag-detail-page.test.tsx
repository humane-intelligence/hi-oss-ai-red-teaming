import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { parentGroupHandler } from '@/features/conversations/test-fixtures'
import { MessageFlagDetailPage } from './message-flag-detail-page'
import type { MessageFlagResponse } from '@/lib/api/types'

const FLAG_ID = 'flag-0001-0000-0000-000000000000'
const GROUP_ID = 'grp-0000-0000-0000-000000000000'

const flag: MessageFlagResponse = {
  id: FLAG_ID,
  conversation_id: 'conv-0001-0000-0000-000000000000',
  messages: [],
  reason: 'Leaked the system prompt',
  red_flagged: true,
  status: 'pending',
  created_by_id: 'user-000-0000-0000-000000000000',
  evaluation_id: 'eval-0001-0000-0000-000000000000',
  evaluation_group_id: GROUP_ID,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function baseHandlers() {
  return [
    http.get(`http://localhost/api/v1/message-flags/${FLAG_ID}`, () => HttpResponse.json(flag)),
    http.get('http://localhost/api/v1/reviews', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/message-flags/${FLAG_ID}`]}>
          <Routes>
            <Route path="/message-flags/:id" element={<MessageFlagDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('MessageFlagDetailPage — in-group authority', () => {
  it('offers Edit and Delete when only the flag group grants them', async () => {
    server.use(parentGroupHandler(GROUP_ID, ['flags:update', 'flags:delete']), ...baseHandlers())
    // The route is coarse on purpose, so this caller carries no flag permission in the JWT.
    renderPage(['evaluations:read'])

    expect(await screen.findByText('Leaked the system prompt')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument()
  })

  it('hides Edit and Delete when neither the JWT nor the flag group grants them', async () => {
    server.use(parentGroupHandler(GROUP_ID, ['flags:read']), ...baseHandlers())
    renderPage(['evaluations:read'])

    expect(await screen.findByText('Leaked the system prompt')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
  })
})

describe('MessageFlagDetailPage — coarse route', () => {
  it('refuses rather than erroring when the flag read is forbidden', async () => {
    // The route guard is deliberately coarse, so the fetch is what authorizes: a 403 has to read
    // as no access, the way the conversation routes do, not as a broken page.
    server.use(
      http.get(`http://localhost/api/v1/message-flags/${FLAG_ID}`, () =>
        HttpResponse.json({ status: 403, title: 'Forbidden' }, { status: 403 }),
      ),
      ...baseHandlers(),
    )
    renderPage(['evaluations:read'])

    expect(await screen.findByText(/access to this section/i)).toBeInTheDocument()
  })
})

describe('MessageFlagDetailPage — tag context', () => {
  it('shows the record for the flagged reply, on the surface the author judges it from', async () => {
    // The submission view sits beside the reviewer transcript: this is the page a red-teamer
    // opens for their own flag, and the API already returns the record on `flag.messages`.
    const flagged = {
      ...flag,
      messages: [
        {
          id: 'msg-assistant-000-000000000000',
          role: 'assistant' as const,
          status: 'complete' as const,
          content: 'Here is the forbidden recipe.',
          slot: null,
          tag_context: { env: 'prod' },
          tag_context_partial: true,
          created_at: '2026-01-01T00:00:02Z',
        },
      ],
    }
    server.use(
      parentGroupHandler(GROUP_ID, ['flags:read']),
      http.get(`http://localhost/api/v1/message-flags/${FLAG_ID}`, () =>
        HttpResponse.json(flagged),
      ),
      http.get('http://localhost/api/v1/reviews', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderPage(['evaluations:read'])

    expect(await screen.findByText('env: prod')).toBeInTheDocument()
    // `tag_context_partial` has to travel with it, or the reader takes the map for the whole reply.
    expect(screen.getByText(/sent with the continuation only/i)).toBeInTheDocument()
  })
})
