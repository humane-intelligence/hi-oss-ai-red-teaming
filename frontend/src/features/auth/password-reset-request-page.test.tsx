import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { PasswordResetRequestPage } from './password-reset-request-page'

describe('PasswordResetRequestPage', () => {
  it('shows a neutral confirmation after submit', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/password-resets/request',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<PasswordResetRequestPage />)
    await user.type(screen.getByLabelText('Email'), 'test@example.com')
    await user.click(screen.getByRole('button', { name: /send reset link/i }))
    expect(await screen.findByText(/if an account exists/i)).toBeInTheDocument()
    // A throttled request is a silent no-op, so the flat "we sent a reset link" was sometimes
    // false; the copy has to account for the mail that deliberately did not go out.
    expect(await screen.findByText(/rate-limited/i)).toBeInTheDocument()
  })

  it('surfaces a non-422 error as a form-level message', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/password-resets/request',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<PasswordResetRequestPage />)
    await user.type(screen.getByLabelText('Email'), 'test@example.com')
    await user.click(screen.getByRole('button', { name: /send reset link/i }))
    expect(await screen.findByText('Request failed (500)')).toBeInTheDocument()
  })
})
