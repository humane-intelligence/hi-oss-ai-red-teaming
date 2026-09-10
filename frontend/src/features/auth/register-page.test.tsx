import { describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { toast } from 'sonner'
import { AuthProvider } from '@/lib/auth/auth-provider'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { RegisterPage, RESEND_COOLDOWN_SECONDS } from './register-page'
import { useSignupEnabled } from '@/features/system-preferences/queries'

function SignupFlagProbe() {
  return <span>{`signup:${useSignupEnabled() ? 'on' : 'off'}`}</span>
}

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const fill = async (user: ReturnType<typeof userEvent.setup>, password = 'longenough123') => {
  await user.type(screen.getByLabelText('Email'), 'test@example.com')
  await user.type(screen.getByLabelText('Password', { selector: '#password' }), password)
}

describe('RegisterPage', () => {
  it('replaces the form with an invite-only notice when signup is disabled', async () => {
    server.use(
      http.get('http://localhost/api/v1/platform-settings/public', () =>
        HttpResponse.json({ signup_enabled: false }),
      ),
    )
    renderWithProviders(<RegisterPage />)

    expect(await screen.findByText('Registration is invite-only')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create account/i })).not.toBeInTheDocument()
  })

  it('resends the verification link with the registered email', async () => {
    let captured: Record<string, unknown> | null = null
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register',
        () => new HttpResponse(null, { status: 202 }),
      ),
      http.post('http://localhost/api/v1/auth/register/resend', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return new HttpResponse(null, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))

    await user.click(await screen.findByRole('button', { name: /resend link/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({ email: 'test@example.com' })
    // The enumeration-safety property lives in this copy: state-agnostic ("if"), and it warns
    // that earlier links die — pin it so it can't drift into confirming account existence.
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        expect.stringMatching(/if the address has a pending sign-up.*earlier links stop working/i),
      ),
    )
  })

  it('holds the resend button on a cooldown after a successful resend', async () => {
    // A second click revokes the link the first one just mailed, so the button must not
    // re-enable the instant the request returns.
    let resends = 0
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register',
        () => new HttpResponse(null, { status: 202 }),
      ),
      http.post('http://localhost/api/v1/auth/register/resend', () => {
        resends += 1
        return new HttpResponse(null, { status: 202 })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      renderWithProviders(<RegisterPage />)
      await fill(user)
      await user.click(screen.getByRole('button', { name: /create account/i }))
      await user.click(await screen.findByRole('button', { name: /resend link/i }))

      const cooling = await screen.findByRole('button', { name: /resend available in \d+s/i })
      expect(cooling).toBeDisabled()
      await user.click(cooling)
      expect(resends).toBe(1)

      await act(async () => {
        vi.advanceTimersByTime(RESEND_COOLDOWN_SECONDS * 1000)
      })
      expect(await screen.findByRole('button', { name: /resend link/i })).toBeEnabled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('blocks submit and shows an error for an empty password', async () => {
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await user.type(screen.getByLabelText('Email'), 'test@example.com')
    // The 10 is the default handler's published minimum, not the shipped fallback the schema is
    // built from until the read settles — so clicking before that pins nothing.
    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    await waitFor(() => expect(requirements).toHaveTextContent('At least 10 characters'))

    await user.click(screen.getByRole('button', { name: /create account/i }))
    // Scoped to the field error (role=alert): the requirements list carries the same sentence.
    expect(await screen.findByRole('alert')).toHaveTextContent('At least 10 characters')
  })

  it('states the published password rules and enforces them before submitting', async () => {
    server.use(
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
      http.post('http://localhost/api/v1/auth/register', () => {
        posted = true
        return new HttpResponse(null, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)

    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    await waitFor(() => expect(requirements).toHaveTextContent('At least 12 characters'))
    expect(requirements).toHaveTextContent('A digit')
    // The rules have to reach a screen reader on the field, not just sighted users beside it.
    expect(screen.getByLabelText('Password', { selector: '#password' })).toHaveAttribute(
      'aria-describedby',
      requirements.id,
    )

    await fill(user, 'nodigitsatall')
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByText('Add a digit')).toBeInTheDocument()
    expect(posted).toBe(false)
  })

  it('falls back to the shipped rules when the public settings are unreachable', async () => {
    // Tightening on a failed read would lock a visitor out of a form the backend would accept.
    // The shipped floor (8) differs from the default handler's (10), so this can only pass once
    // the query has settled into its error state — the earlier version went green either way.
    server.use(
      http.get(
        'http://localhost/api/v1/platform-settings/public',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )
    renderWithProviders(<RegisterPage />)

    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    await waitFor(() => expect(requirements).toHaveTextContent('At least 8 characters'))
    expect(requirements).not.toHaveTextContent('At least 10 characters')
    expect(requirements).not.toHaveTextContent('A digit')
  })

  it('withholds the rules until the published policy has settled, but not the submit', async () => {
    // The fallback is indistinguishable from a real shipped-policy read, so rendering it while the
    // query is still pending would state the looser rules and then flip. The submit deliberately
    // stays live: this read fails open, and a hung one would otherwise disable an anonymous
    // recovery screen outright. A password the configured policy refuses comes back as a 400
    // mapped onto the field, the same route the two unconditional rules already take. What is
    // pinned here is that pending window; a hang leaves it via the read's deadline and lands in the
    // fallback the test above covers.
    server.use(
      http.get('http://localhost/api/v1/platform-settings/public', async () => {
        await delay('infinite')
        return HttpResponse.json({})
      }),
    )
    renderWithProviders(<RegisterPage />)

    const requirements = await screen.findByRole('list', { name: 'Password requirements' })
    expect(requirements).toHaveTextContent(
      'Checking the password rules — the server enforces them regardless.',
    )
    expect(requirements).not.toHaveTextContent('At least 8 characters')
    // Once the consent read has settled — that one the submit does wait on, since a signup that
    // cannot report consent is refused.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /create account/i })).toBeEnabled(),
    )
  })

  it('shows a confirmation after a successful submit', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register',
        () => new HttpResponse(null, { status: 202 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByText('Check your email')).toBeInTheDocument()
  })

  it('keeps the confirmation when invite-only is switched on mid-view', async () => {
    let signupEnabled = true
    server.use(
      http.get('http://localhost/api/v1/platform-settings/public', () =>
        HttpResponse.json({ signup_enabled: signupEnabled }),
      ),
      http.post(
        'http://localhost/api/v1/auth/register',
        () => new HttpResponse(null, { status: 202 }),
      ),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter>
            <RegisterPage />
            <SignupFlagProbe />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    )

    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByText('Check your email')).toBeInTheDocument()

    // An admin flips the toggle and a refetch (on reconnect, say) lands mid-view. The probe is
    // what makes the flip observable — subscribers are notified a tick after the invalidation
    // settles, so asserting straight after it would pass against the old ordering too.
    signupEnabled = false
    await act(() => qc.invalidateQueries({ queryKey: ['platform-settings'] }))
    await screen.findByText('signup:off')

    expect(screen.getByText('Check your email')).toBeInTheDocument()
    expect(screen.queryByText('Registration is invite-only')).not.toBeInTheDocument()
  })

  it('maps a 422 onto the email field', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/register', () =>
        HttpResponse.json(
          {
            errors: [
              { loc: ['body', 'email'], msg: 'Email already registered', type: 'value_error' },
            ],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByText('Email already registered')).toBeInTheDocument()
  })

  it('maps a 400 password-policy rejection onto the password field', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/register', () =>
        HttpResponse.json(
          {
            errors: [
              {
                loc: ['body', 'password'],
                msg: 'This password is too common.',
                type: 'password_too_common',
              },
            ],
          },
          { status: 400 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByText('This password is too common.')).toBeInTheDocument()
  })

  it('surfaces a non-422 error as a form-level message', async () => {
    server.use(
      http.post(
        'http://localhost/api/v1/auth/register',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByText('Request failed (500)')).toBeInTheDocument()
  })
})

describe('RegisterPage — terms consent', () => {
  const DOCUMENT = {
    id: 'terms-1',
    version: '2.1',
    content: '# Terms\n\nNo jailbreaking the staff.',
    published_at: '2026-09-02T12:00:00Z',
  }

  const publishedTerms = () =>
    server.use(http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(DOCUMENT)))

  it('offers no consent checkbox while nothing is published', async () => {
    renderWithProviders(<RegisterPage />)

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(screen.queryByLabelText(/accept the terms/i)).not.toBeInTheDocument()
    // The optional one is not tied to a published version, so it always shows.
    expect(screen.getByLabelText(/send me product email/i)).toBeInTheDocument()
  })

  it('holds the submit while the consent read is in flight', async () => {
    // The window the checkbox is not mounted in: a submit here is refused for consent the form
    // cannot report, and the refusal maps onto a field that is not on screen.
    server.use(
      http.get('http://localhost/api/v1/terms/current', async () => {
        await delay('infinite')
        return HttpResponse.json(DOCUMENT)
      }),
    )

    renderWithProviders(<RegisterPage />)

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create account/i })).toBeDisabled()
    expect(screen.getByText('Checking the terms of service…')).toBeInTheDocument()
  })

  it('closes the terms dialog with a button that cannot submit this form', async () => {
    // `Modal` renders its <dialog> inline, so an untyped button inside it is a submit button owned
    // by the registration form. The guard is the attribute: jsdom never reaches the activation
    // path, because React unmounts the button while flushing the click.
    publishedTerms()
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/register', () => {
        posted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)

    await user.click(await screen.findByRole('button', { name: /read the terms of service/i }))
    const dialog = await screen.findByRole('dialog')
    const close = within(dialog).getByRole('button', { name: 'Close' })
    expect(close).toHaveAttribute('type', 'button')

    await user.click(close)

    expect(posted).toBe(false)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('refuses to submit until the published terms are accepted', async () => {
    publishedTerms()
    let posted = false
    server.use(
      http.post('http://localhost/api/v1/auth/register', () => {
        posted = true
        return new HttpResponse(null, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await screen.findByLabelText(/accept the terms/i)

    await fill(user)
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/must accept the terms/i)
    expect(posted).toBe(false)
  })

  it('sends the consent flags and the rendered version once accepted', async () => {
    publishedTerms()
    let body: Record<string, unknown> | null = null
    server.use(
      http.post('http://localhost/api/v1/auth/register', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return new HttpResponse(null, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await screen.findByLabelText(/accept the terms/i)

    await fill(user)
    await user.click(screen.getByLabelText(/accept the terms/i))
    await user.click(screen.getByLabelText(/send me product email/i))
    await user.click(screen.getByRole('button', { name: /create account/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({
      consent_terms: true,
      consent_emails: true,
      terms_id: 'terms-1',
    })
  })

  it('shows the version and opens the full text in a dialog', async () => {
    publishedTerms()
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)

    expect(await screen.findByText(/Version 2\.1 — required to continue/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /read the terms of service/i }))

    const region = await screen.findByRole('region', { name: 'Terms of service, version 2.1' })
    expect(region).toHaveTextContent('No jailbreaking the staff')
  })
})

describe('RegisterPage — terms read failures', () => {
  it('blocks the submit and says so when the terms cannot be read', async () => {
    server.use(
      http.get(
        'http://localhost/api/v1/terms/current',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )
    renderWithProviders(<RegisterPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load the terms/)
    // Posting here would be refused for a field this form isn't rendering — reported nowhere.
    expect(screen.getByRole('button', { name: /create account/i })).toBeDisabled()
  })

  it('re-reads the terms and clears the tick when the version moved under the form', async () => {
    const v1 = {
      id: 'terms-1',
      version: '1.0',
      content: 'First.',
      published_at: '2026-01-01T00:00:00Z',
    }
    const v2 = {
      id: 'terms-2',
      version: '2.0',
      content: 'Second.',
      published_at: '2026-06-01T00:00:00Z',
    }
    let current = v1
    server.use(
      http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(current)),
      http.post('http://localhost/api/v1/auth/register', () => {
        current = v2
        return HttpResponse.json(
          { title: 'Conflict', status: 409, detail: 'stale' },
          { status: 409 },
        )
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<RegisterPage />)
    await screen.findByLabelText(/accept the terms/i)

    await fill(user)
    await user.click(screen.getByLabelText(/accept the terms/i))
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByText(/terms of service changed/i)).toBeInTheDocument()
    // The newer text is on screen and the tick is gone — a resubmit cannot carry consent over.
    expect(await screen.findByText(/Version 2\.0/)).toBeInTheDocument()
    expect(screen.getByLabelText(/accept the terms/i)).not.toBeChecked()
  })
})
