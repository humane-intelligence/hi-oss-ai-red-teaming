import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { LoginPage } from './login-page'

const providersEmpty = http.get('http://localhost/api/v1/auth/oidc/providers', () =>
  HttpResponse.json([]),
)

describe('LoginPage', () => {
  it('signs in and navigates to the overview', async () => {
    server.use(
      providersEmpty,
      http.post('http://localhost/api/v1/auth/login', () =>
        HttpResponse.json({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }),
      ),
      http.get('http://localhost/api/v1/auth/me', () =>
        HttpResponse.json({
          id: '1',
          email: 'test@example.com',
          email_verified: true,
          provider: 'local',
        }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<div>OVERVIEW</div>} />
      </Routes>,
      { route: '/login' },
    )
    await user.type(screen.getByLabelText('Email'), 'test@example.com')
    await user.type(screen.getByLabelText('Password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('OVERVIEW')).toBeInTheDocument()
  })

  it('offers sign-up while open registration is enabled', async () => {
    server.use(providersEmpty)
    renderWithProviders(<LoginPage />, { route: '/login' })

    expect(await screen.findByRole('link', { name: 'Create one' })).toBeInTheDocument()
  })

  it('hides the sign-up link under invite-only mode', async () => {
    server.use(
      providersEmpty,
      http.get('http://localhost/api/v1/platform-settings/public', () =>
        HttpResponse.json({ signup_enabled: false }),
      ),
    )
    renderWithProviders(<LoginPage />, { route: '/login' })

    // The link renders fail-open on first paint, so assert its *removal* once the flag
    // resolves — a bare absence check could pass before the query ever ran.
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: 'Create one' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toBeInTheDocument()
  })

  it('shows an error on invalid credentials', async () => {
    server.use(
      providersEmpty,
      http.post(
        'http://localhost/api/v1/auth/login',
        () => new HttpResponse(null, { status: 401 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />, { route: '/login' })
    await user.type(screen.getByLabelText('Email'), 'test@example.com')
    await user.type(screen.getByLabelText('Password'), 'longenough123')
    await user.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Invalid email or password.')).toBeInTheDocument()
  })
})
