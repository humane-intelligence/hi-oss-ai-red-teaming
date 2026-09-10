import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { OidcButtons } from './oidc-buttons'

describe('OidcButtons', () => {
  it('renders a button per provider', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/oidc/providers', () =>
        HttpResponse.json(['google', 'github']),
      ),
    )
    renderWithProviders(<OidcButtons />)
    expect(await screen.findByRole('button', { name: /continue with google/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /continue with github/i })).toBeInTheDocument()
  })

  it('redirects to the provider login endpoint on click', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/oidc/providers', () => HttpResponse.json(['google'])),
    )
    const originalLocation = window.location
    const assign = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, assign },
    })
    const user = userEvent.setup()
    renderWithProviders(<OidcButtons />)
    await user.click(await screen.findByRole('button', { name: /continue with google/i }))
    expect(assign).toHaveBeenCalledWith('http://localhost/api/v1/auth/oidc/google/login')
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  })
})
