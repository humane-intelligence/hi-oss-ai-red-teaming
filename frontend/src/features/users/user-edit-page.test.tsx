import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { UserEditPage } from './user-edit-page'
import type { UserResponse } from '@/lib/api/types'

const USER: UserResponse = {
  id: 'user-0003',
  email: 'carol@example.com',
  status: 'invited',
  email_verified: false,
  invitation: { status: 'pending', expires_at: '2026-08-15T12:00:00Z' },
  roles: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function renderPage(user: UserResponse = USER) {
  server.use(
    http.get('http://localhost/api/v1/auth/users/:id', () => HttpResponse.json(user)),
    http.get('http://localhost/api/v1/auth/roles', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  )
  const Wrapper = authWrapper(['users:read', 'users:update'])
  render(
    <Wrapper>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter initialEntries={[`/users/${user.id}/edit`]}>
          <Routes>
            <Route path="/users/:id/edit" element={<UserEditPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('UserEditPage account state', () => {
  // This block is what lets the list hide both below `xl`; nothing else states them.
  it('carries the invitation and its expiry, which the list hides below xl', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('Account state')).toBeInTheDocument())
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByText(/expires/i)).toBeInTheDocument()
  })

  it('states whether the email is verified', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('Account state')).toBeInTheDocument())
    expect(screen.getByText('Email verified')).toBeInTheDocument()
    expect(screen.getByText('No')).toBeInTheDocument()
  })

  it('says so plainly when there is no invitation', async () => {
    renderPage({ ...USER, status: 'active', email_verified: true, invitation: undefined })

    await waitFor(() => expect(screen.getByText('Account state')).toBeInTheDocument())
    expect(screen.getByText('none')).toBeInTheDocument()
    expect(screen.getByText('Yes')).toBeInTheDocument()
  })
})
