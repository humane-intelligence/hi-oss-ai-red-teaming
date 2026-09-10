import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { PasswordResetConfirmPage } from './password-reset-confirm-page'

const withLoginRoute = (
  <Routes>
    <Route path="/password-reset/confirm" element={<PasswordResetConfirmPage />} />
    <Route path="/login" element={<div>LOGIN</div>} />
  </Routes>
)

describe('PasswordResetConfirmPage', () => {
  it('shows an invalid-link message when no token is present', () => {
    renderWithProviders(<PasswordResetConfirmPage />, { route: '/password-reset/confirm' })
    expect(screen.getByText('Invalid reset link')).toBeInTheDocument()
  })

  it('navigates to login after a successful reset', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/password-resets/confirm',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(withLoginRoute, { route: '/password-reset/confirm?token=t' })
    await user.type(screen.getByLabelText('New password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /update password/i }))
    expect(await screen.findByText('LOGIN')).toBeInTheDocument()
  })

  it('enforces the published policy locally, once it has arrived', async () => {
    // The API would refuse this password anyway (the token survives — the confirm is
    // transactional and validates before marking it used); catching it here saves the trip.
    // The 12 matches neither the shipped fallback (8) nor the shared handler's default (10), and
    // it is asserted below, so the list can only be showing this override.
    server.use(
      http.get('http://localhost/api/v1/platform-settings/public', () =>
        HttpResponse.json({
          signup_enabled: true,
          password_policy: {
            min_length: 12,
            require_uppercase: true,
            require_digit: false,
            require_symbol: false,
          },
        }),
      ),
    )
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/password-resets/confirm', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(withLoginRoute, { route: '/password-reset/confirm?token=t' })

    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    await waitFor(() => expect(requirements).toHaveTextContent('At least 12 characters'))
    expect(requirements).toHaveTextContent('An uppercase letter')

    await user.type(screen.getByLabelText('New password'), 'alllowercase1')
    await user.click(screen.getByRole('button', { name: /update password/i }))

    expect(await screen.findByText('Add an uppercase letter')).toBeInTheDocument()
    expect(posted).toBe(false)
  })

  it('maps a 422 onto the password field', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/password-resets/confirm', () =>
        HttpResponse.json(
          {
            errors: [{ loc: ['body', 'password'], msg: 'Password too weak', type: 'value_error' }],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<PasswordResetConfirmPage />, { route: '/password-reset/confirm?token=t' })
    await user.type(screen.getByLabelText('New password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /update password/i }))
    expect(await screen.findByText('Password too weak')).toBeInTheDocument()
  })
})
