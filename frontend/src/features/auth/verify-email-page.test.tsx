import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { VerifyEmailPage } from './verify-email-page'

describe('VerifyEmailPage', () => {
  it('shows failure when no token is present', () => {
    renderWithProviders(<VerifyEmailPage />, { route: '/register/verify' })
    expect(screen.getByText('Verification failed')).toBeInTheDocument()
  })

  it('shows success when the token verifies', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register/verify',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    renderWithProviders(<VerifyEmailPage />, { route: '/register/verify?token=good' })
    expect(await screen.findByText('Email verified')).toBeInTheDocument()
  })

  it('shows failure when the token is rejected', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register/verify',
        () => new HttpResponse(null, { status: 400 }),
      ),
    )
    renderWithProviders(<VerifyEmailPage />, { route: '/register/verify?token=bad' })
    expect(await screen.findByText('Verification failed')).toBeInTheDocument()
  })
})
