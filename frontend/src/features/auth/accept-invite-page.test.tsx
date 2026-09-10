import { describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { AcceptInvitePage } from './accept-invite-page'

const preview = {
  email: 'invited@example.com',
  inviter_name: 'Alice',
  role_names: ['annotator'],
  expires_at: '2026-12-31T00:00:00Z',
}

describe('AcceptInvitePage', () => {
  it('shows invalid when no token is present', () => {
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept' })
    expect(screen.getByText('Invitation invalid')).toBeInTheDocument()
  })

  it('shows the invited email and roles from the preview', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
    )
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })
    expect(await screen.findByDisplayValue('invited@example.com')).toBeInTheDocument()
    expect(screen.getByText(/annotator/i)).toBeInTheDocument()
  })

  it('shows invalid when the preview request fails', async () => {
    server.use(
      http.get(
        'http://localhost/api/v1/auth/invitations/accept',
        () => new HttpResponse(null, { status: 404 }),
      ),
    )
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })
    expect(await screen.findByText('Invitation invalid')).toBeInTheDocument()
  })

  it('navigates to login after accepting', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
      http.post(
        'http://localhost/api/v1/auth/invitations/accept',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(
      <Routes>
        <Route path="/invite/accept" element={<AcceptInvitePage />} />
        <Route path="/login" element={<div>LOGIN</div>} />
      </Routes>,
      { route: '/invite/accept?token=t' },
    )
    await user.type(await screen.findByLabelText('Password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /accept/i }))
    expect(await screen.findByText('LOGIN')).toBeInTheDocument()
  })

  it('states the published password policy and refuses a violation without posting', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
      http.get('http://localhost/api/v1/platform-settings/public', () =>
        HttpResponse.json({
          signup_enabled: true,
          password_policy: {
            min_length: 12,
            require_uppercase: false,
            require_digit: true,
            require_symbol: false,
          },
        }),
      ),
    )
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/invitations/accept', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })

    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    await waitFor(() => expect(requirements).toHaveTextContent('At least 12 characters'))
    expect(requirements).toHaveTextContent('A digit')
    expect(await screen.findByLabelText('Password')).toHaveAttribute(
      'aria-describedby',
      requirements.id,
    )

    await user.type(await screen.findByLabelText('Password'), 'nodigitsatall')
    await user.click(screen.getByRole('button', { name: /accept/i }))

    expect(await screen.findByText('Add a digit')).toBeInTheDocument()
    expect(posted).toBe(false)
  })
})

describe('AcceptInvitePage — terms consent', () => {
  const DOCUMENT = {
    id: 'terms-1',
    version: '3.0',
    content: 'The rules of engagement.',
    published_at: '2026-09-02T12:00:00Z',
  }

  const withTerms = () =>
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
      http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(DOCUMENT)),
    )

  it('holds the submit while the consent read is in flight', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
      http.get('http://localhost/api/v1/terms/current', async () => {
        await delay('infinite')
        return HttpResponse.json(DOCUMENT)
      }),
    )

    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /accept & set password/i })).toBeDisabled()
    expect(screen.getByText('Checking the terms of service…')).toBeInTheDocument()
  })

  it('refuses to submit until the published terms are accepted', async () => {
    withTerms()
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/invitations/accept', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })

    await user.type(await screen.findByLabelText('Password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /accept & set password/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/must accept the terms/i)
    expect(posted).toBe(false)
  })

  it('sends the consent flags and the rendered version once accepted', async () => {
    withTerms()
    let body: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/auth/invitations/accept', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })

    await user.type(await screen.findByLabelText('Password'), 'longenough123')
    await user.click(screen.getByLabelText(/accept the terms/i))
    await user.click(screen.getByRole('button', { name: /accept & set password/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({ consent_terms: true, consent_emails: false, terms_id: 'terms-1' })
  })

  it('offers no terms checkbox while nothing is published', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/invitations/accept', () => HttpResponse.json(preview)),
    )
    renderWithProviders(<AcceptInvitePage />, { route: '/invite/accept?token=t' })

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.queryByLabelText(/accept the terms/i)).not.toBeInTheDocument()
  })
})
