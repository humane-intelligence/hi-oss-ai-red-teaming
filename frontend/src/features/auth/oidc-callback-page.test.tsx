import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { tokenStore } from '@/lib/auth/token-store'
import { OidcCallbackPage } from './oidc-callback-page'

const me = http.get('http://localhost/api/v1/auth/me', () =>
  HttpResponse.json({
    id: '1',
    email: 'ada@example.com',
    email_verified: true,
    provider: 'google',
  }),
)

function renderCallback(fragment: string, { strict = false }: { strict?: boolean } = {}) {
  return renderWithProviders(
    <Routes>
      <Route path="/auth/callback" element={<OidcCallbackPage />} />
      <Route path="/" element={<div>OVERVIEW</div>} />
      <Route path="/login" element={<div>LOGIN</div>} />
    </Routes>,
    { route: `/auth/callback${fragment}`, strict },
  )
}

afterEach(() => {
  tokenStore.clear()
})

describe('OidcCallbackPage', () => {
  it('adopts the session from the fragment and lands on the overview', async () => {
    server.use(me)
    renderCallback('#access_token=jwt-from-idp&refresh_token=r&expires_in=3600')

    expect(await screen.findByText('OVERVIEW')).toBeInTheDocument()
    expect(tokenStore.get()).toBe('jwt-from-idp')
  })

  it('strips the token from the URL so it cannot be recovered from history', async () => {
    // jsdom's `window.location` is pinned to `http://localhost` regardless of the
    // `MemoryRouter` route (see vitest.config.ts), so the fragment never actually
    // reaches it in this test either way — asserting the *shape* of the replaceState
    // call (built from `pathname` + `search`, not `.href` or the fragment verbatim)
    // is what actually distinguishes a correct strip from a no-op.
    server.use(me)
    const replaceState = vi.spyOn(window.history, 'replaceState')
    renderCallback('#access_token=jwt-from-idp&expires_in=3600')

    expect(await screen.findByText('OVERVIEW')).toBeInTheDocument()
    expect(replaceState).toHaveBeenCalledTimes(1)
    const [, title, url] = replaceState.mock.calls[0]!
    expect(title).toBe('')
    expect(url).toBe('/')
    replaceState.mockRestore()
  })

  it('preserves the existing history state instead of wiping it', async () => {
    // React Router's own `{usr, key, idx}` bookkeeping lives on `history.state` —
    // passing `null` here would corrupt it instead of just stripping the URL. Seed a
    // real, distinguishable state first: jsdom's `history.state` starts `null`, so
    // without this a broken `null`-literal version and the fix are indistinguishable
    // (`MemoryRouter` never touches real `window.history`, so nothing else sets it).
    server.use(me)
    window.history.replaceState({ usr: null, key: 'probe', idx: 0 }, '', window.location.pathname)
    const existingState = window.history.state
    const replaceState = vi.spyOn(window.history, 'replaceState')
    renderCallback('#access_token=jwt-from-idp&expires_in=3600')

    expect(await screen.findByText('OVERVIEW')).toBeInTheDocument()
    expect(replaceState).toHaveBeenCalledWith(existingState, '', expect.any(String))
    replaceState.mockRestore()
  })

  it('explains a provider-side refusal instead of stranding the user', async () => {
    renderCallback('#error=access_denied')

    expect(await screen.findByText(/did not complete the sign-in/i)).toBeInTheDocument()
    expect(tokenStore.get()).toBeNull()
  })

  it('falls back to generic copy for an unrecognised error code', async () => {
    renderCallback('#error=teapot')

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument()
  })

  it('treats a fragment with no token as a failure', async () => {
    renderCallback('')

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument()
  })

  it('fails closed when the token is rejected by /me', async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/me', () => new HttpResponse(null, { status: 401 })),
    )
    renderCallback('#access_token=stale-jwt')

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument()
    expect(tokenStore.get()).toBeNull()
  })

  it('does not leave a live token behind when /me fails for a reason other than 401', async () => {
    server.use(http.get('http://localhost/api/v1/auth/me', () => HttpResponse.error()))
    renderCallback('#access_token=jwt-from-idp')

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument()
    expect(tokenStore.get()).toBeNull()
  })

  it('offers a way back to sign in after a failure', async () => {
    renderCallback('#error=account_inactive')

    expect(await screen.findByRole('button', { name: /back to sign in/i })).toBeInTheDocument()
    expect(screen.getByText(/ask an administrator/i)).toBeInTheDocument()
  })

  it('explains an invite-only refusal instead of suggesting a retry', async () => {
    // The backend refuses OIDC first-login provisioning under invite-only; a retry
    // cannot help, so the copy must match the register page's invite-only note.
    renderCallback('#error=invite_only')

    expect(await screen.findByText(/registration is invite-only/i)).toBeInTheDocument()
  })

  it('moves focus to the recovery action after a failure', async () => {
    renderCallback('#error=account_inactive')

    const button = await screen.findByRole('button', { name: /back to sign in/i })
    expect(button).toHaveFocus()
  })

  it('describes the failure to the focused recovery action', async () => {
    // The failure copy is already the first render for `#error=` — the aria-live region
    // never mutates, so it never announces. `aria-describedby` on the focused button
    // carries the reason instead, independent of live-region timing.
    renderCallback('#error=account_inactive')

    const button = await screen.findByRole('button', { name: /back to sign in/i })
    expect(button).toHaveAccessibleDescription(/ask an administrator/i)
  })

  it('still lands on the overview under StrictMode', async () => {
    // Regression: a mount→cleanup→remount cycle must not leave the closure that
    // adoptSession() resolves into permanently disarmed — see write-note-dialog.tsx.
    server.use(me)
    renderCallback('#access_token=jwt-from-idp&expires_in=3600', { strict: true })

    expect(await screen.findByText('OVERVIEW')).toBeInTheDocument()
    expect(tokenStore.get()).toBe('jwt-from-idp')
  })
})
