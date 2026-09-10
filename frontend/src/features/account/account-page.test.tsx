import { describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AccountPage } from './account-page'
import type { MeResponse, MeUpdate } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function renderPage(user: Partial<MeResponse> = {}, logout: () => void = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper([], [], null, {
    user: { first_name: 'Ada', last_name: 'Lovelace', ...user },
    logout,
  })
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/account']}>
          <AccountPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

async function fillPasswords(current: string, next: string, confirm = next) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Current password'), current)
  await user.type(screen.getByLabelText('New password'), next)
  await user.type(screen.getByLabelText('Confirm new password'), confirm)
  await user.click(screen.getByRole('button', { name: 'Change password' }))
}

describe('AccountPage', () => {
  it('prefills the profile from the held identity', () => {
    renderPage()

    expect(screen.getByLabelText('First name')).toHaveValue('Ada')
    expect(screen.getByLabelText('Last name')).toHaveValue('Lovelace')
    // Pristine form — nothing to save yet.
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('PATCHes the edited name, sending an explicit null for a cleared field', async () => {
    let body: MeUpdate | null = null
    server.use(
      http.patch('http://localhost/api/v1/auth/me', async ({ request }) => {
        body = (await request.json()) as MeUpdate
        return HttpResponse.json({
          id: '1',
          email: 'a@b.c',
          provider: 'local',
          email_verified: true,
          has_password: true,
          first_name: 'Augusta',
          last_name: null,
          roles: [],
          permissions: [],
          organization: null,
        })
      }),
    )
    const user = userEvent.setup()
    renderPage()

    await user.clear(screen.getByLabelText('First name'))
    await user.type(screen.getByLabelText('First name'), 'Augusta')
    await user.clear(screen.getByLabelText('Last name'))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(body).toEqual({ first_name: 'Augusta', last_name: null }))
  })

  it('refuses an over-length name inline instead of failing silently', async () => {
    let patched = false
    server.use(
      http.patch('http://localhost/api/v1/auth/me', () => {
        patched = true
        return new HttpResponse(null, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    renderPage()

    await user.click(screen.getByLabelText('First name'))
    await user.paste('a'.repeat(256))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('At most 255 characters')).toBeInTheDocument()
    expect(patched).toBe(false)
  })

  it('surfaces an errors[] entry it does not own as a form-level alert', async () => {
    // The whitelist keeps an unowned entry from counting as mapped; the mutation opts out
    // of the global toast, so this alert is the only channel left.
    server.use(
      http.patch('http://localhost/api/v1/auth/me', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            detail: 'Validation failed.',
            errors: [
              { loc: ['body', 'role_ids'], msg: 'Not permitted here.', type: 'extra_forbidden' },
            ],
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    renderPage()

    await user.clear(screen.getByLabelText('First name'))
    await user.type(screen.getByLabelText('First name'), 'Augusta')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Validation failed.')
  })

  it('toasts a failure that lands after the card is gone', async () => {
    // The mutation opts out of the global toast, so an unmounted card would otherwise
    // report a lost save to nobody (frontend/CLAUDE.md: the opt-out owes this fallback).
    server.use(
      http.patch('http://localhost/api/v1/auth/me', async () => {
        await delay(20)
        return new HttpResponse(null, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    const { unmount } = renderPage()

    await user.clear(screen.getByLabelText('First name'))
    await user.type(screen.getByLabelText('First name'), 'Augusta')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    unmount()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Something went wrong on the server. Try again in a moment.',
      ),
    )
  })

  it('POSTs the password change without the confirmation field', async () => {
    let body: unknown = null
    server.use(
      http.post('http://localhost/api/v1/auth/me/password', async ({ request }) => {
        body = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderPage()

    await fillPasswords('old-password-123', 'brand-new-pass-456')

    await waitFor(() =>
      expect(body).toEqual({
        current_password: 'old-password-123',
        password: 'brand-new-pass-456',
      }),
    )
  })

  it('signs the user out after a successful change', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/me/password',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const logout = vi.fn()
    renderPage({}, logout)

    await fillPasswords('old-password-123', 'brand-new-pass-456')

    await waitFor(() => expect(logout).toHaveBeenCalledOnce())
  })

  it('blocks a mismatched confirmation without posting', async () => {
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/me/password', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderPage()

    await fillPasswords('old-password-123', 'brand-new-pass-456', 'brand-new-pass-457')

    expect(await screen.findByText('Passwords do not match')).toBeInTheDocument()
    expect(posted).toBe(false)
  })

  it('maps a wrong current password onto the field instead of a global failure', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/me/password', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Bad Request',
            status: 400,
            detail: 'Current password is incorrect.',
            errors: [
              {
                loc: ['body', 'current_password'],
                msg: 'Current password is incorrect.',
                type: 'current_password_incorrect',
              },
            ],
          },
          { status: 400, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage()

    await fillPasswords('not-the-password', 'brand-new-pass-456')

    expect(await screen.findByText('Current password is incorrect.')).toBeInTheDocument()
  })

  it('surfaces a 409 from a stale passwordless state as a form error', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/me/password', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'The account has no local password; use the password-reset flow instead.',
          },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage()

    await fillPasswords('old-password-123', 'brand-new-pass-456')

    expect(
      await screen.findByText(
        'The account has no local password; use the password-reset flow instead.',
      ),
    ).toBeInTheDocument()
  })

  it('states the sign-out consequence before submit', () => {
    renderPage()

    expect(screen.getByText(/signs you out everywhere/i)).toBeInTheDocument()
  })

  it('replaces the password form with a note for a passwordless account', () => {
    renderPage({ has_password: false })

    expect(screen.getByText(/managed by your identity provider/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('Current password')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change password' })).not.toBeInTheDocument()
  })
})
