import { describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { RequireTermsAcceptance } from './terms-gate'
import type { MeResponse } from '@/lib/api/types'

const DOCUMENT = {
  id: 'terms-1',
  version: '1.0',
  content: '# Terms\n\nBe careful with the models.',
  published_at: '2026-09-02T12:00:00Z',
}

function publishedTerms(document = DOCUMENT) {
  server.use(http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(document)))
}

function renderGuard(
  user: Partial<MeResponse>,
  callbacks: { logout?: () => void; updateUser?: (u: MeResponse) => void } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper([], [], null, { user, ...callbacks })
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route element={<RequireTermsAcceptance />}>
              <Route path="/" element={<p>the application</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('RequireTermsAcceptance', () => {
  it('renders the application when no acceptance is owed', () => {
    renderGuard({ terms_acceptance_required: false })

    expect(screen.getByText('the application')).toBeInTheDocument()
  })

  it('replaces the application with the gate when an acceptance is owed', async () => {
    publishedTerms()

    renderGuard({ terms_acceptance_required: true })

    expect(await screen.findByRole('heading', { name: 'Terms of service' })).toBeInTheDocument()
    // The point of a route-level gate over a modal: nothing behind it renders.
    expect(screen.queryByText('the application')).not.toBeInTheDocument()
  })

  it('shows the version and the text in a named, focusable region', async () => {
    publishedTerms()

    renderGuard({ terms_acceptance_required: true })

    expect(await screen.findByText('Version 1.0')).toBeInTheDocument()
    const region = screen.getByRole('region', { name: 'Terms of service, version 1.0' })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(region).toHaveTextContent('Be careful with the models')
  })

  it('accepts the rendered version and adopts the returned identity', async () => {
    publishedTerms()
    let body: { terms_id?: string } | null = null
    const accepted: MeResponse[] = []
    server.use(
      http.post('http://localhost/api/v1/auth/me/terms', async ({ request }) => {
        body = (await request.json()) as { terms_id: string }
        return HttpResponse.json({
          id: 'u1',
          email: 'a@b.c',
          provider: 'local',
          email_verified: true,
          has_password: true,
          roles: [],
          permissions: [],
          organization: null,
          consent_terms: true,
          consent_emails: false,
          terms_accepted_at: '2026-09-02T13:00:00Z',
          terms_acceptance_required: false,
        })
      }),
    )

    renderGuard({ terms_acceptance_required: true }, { updateUser: (u) => void accepted.push(u) })
    await userEvent.click(await screen.findByRole('button', { name: 'I accept' }))

    await waitFor(() => expect(accepted).toHaveLength(1))
    expect(body).toEqual({ terms_id: 'terms-1' })
    expect(accepted[0]?.terms_acceptance_required).toBe(false)
  })

  it('signs out instead of accepting', async () => {
    publishedTerms()
    const logout = vi.fn()

    renderGuard({ terms_acceptance_required: true }, { logout })
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    expect(logout).toHaveBeenCalledOnce()
  })

  it('keeps sign out reachable while an acceptance hangs', async () => {
    // The only other way past this screen, and the accept it waits on may never land.
    publishedTerms()
    server.use(
      http.post('http://localhost/api/v1/auth/me/terms', async () => {
        await delay('infinite')
        return HttpResponse.json({})
      }),
    )
    const logout = vi.fn()

    renderGuard({ terms_acceptance_required: true }, { logout })
    await userEvent.click(await screen.findByRole('button', { name: 'I accept' }))

    expect(await screen.findByRole('button', { name: 'Accepting…' })).toBeDisabled()
    const signOut = screen.getByRole('button', { name: 'Sign out' })
    expect(signOut).toBeEnabled()
    await userEvent.click(signOut)
    expect(logout).toHaveBeenCalledOnce()
  })

  it('re-reads the text and reports the refusal when a newer version landed mid-read', async () => {
    publishedTerms()
    server.use(
      http.post('http://localhost/api/v1/auth/me/terms', () =>
        HttpResponse.json(
          {
            title: 'Conflict',
            status: 409,
            detail: 'Those terms are no longer the current version.',
          },
          { status: 409 },
        ),
      ),
    )

    renderGuard({ terms_acceptance_required: true })
    await userEvent.click(await screen.findByRole('button', { name: 'I accept' }))

    // Curated copy, the same sentence both onboarding forms use — not the backend's detail, which
    // names a version the reader never saw.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The terms of service changed. Please review and accept them again.',
    )
    // Still gated — a refused acceptance must not open the app.
    expect(screen.queryByText('the application')).not.toBeInTheDocument()
  })

  it('fails closed with a retry when the terms cannot be read', async () => {
    server.use(
      http.get(
        'http://localhost/api/v1/terms/current',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )

    renderGuard({ terms_acceptance_required: true })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I accept' })).toBeDisabled()
    expect(screen.queryByText('the application')).not.toBeInTheDocument()
  })

  it('recovers the text when the retry succeeds', async () => {
    let attempt = 0
    server.use(
      http.get('http://localhost/api/v1/terms/current', () => {
        attempt += 1
        return attempt === 1 ? new HttpResponse(null, { status: 500 }) : HttpResponse.json(DOCUMENT)
      }),
    )

    renderGuard({ terms_acceptance_required: true })
    await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('Version 1.0')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I accept' })).toBeEnabled()
  })

  it('lets the app through when the required version has vanished', async () => {
    // Only reachable by deleting the row straight in the database; the alternative is trapping the
    // account behind a gate with nothing to accept.
    renderGuard({ terms_acceptance_required: true })

    expect(await screen.findByText('the application')).toBeInTheDocument()
  })

  it('says the terms changed when the account had already accepted an older version', async () => {
    publishedTerms()

    renderGuard({ terms_acceptance_required: true, consent_terms: true })

    expect(await screen.findByText(/terms have changed/i)).toBeInTheDocument()
  })
})

// The app's real query defaults, unlike the bare client the tests above use: `staleTime: 30_000`
// with no window-focus refetch is what let a superseded version sit on the gate.
describe('RequireTermsAcceptance — against the production query defaults', () => {
  function renderWithAppDefaults(seeded: typeof DOCUMENT) {
    const qc = new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: false, refetchOnWindowFocus: false } },
    })
    qc.setQueryData(['terms', 'current'], seeded)
    const Wrapper = authWrapper([], [], null, { user: { terms_acceptance_required: true } })
    return render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/']}>
            <Routes>
              <Route element={<RequireTermsAcceptance />}>
                <Route path="/" element={<p>the application</p>} />
              </Route>
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </Wrapper>,
    )
  }

  it('shows the newly published version, not the one left in the cache', async () => {
    const v1 = { ...DOCUMENT, id: 'terms-1', version: '1.0', content: 'Old text.' }
    const v2 = { ...DOCUMENT, id: 'terms-2', version: '2.0', content: 'New text.' }
    server.use(http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(v2)))

    renderWithAppDefaults(v1)

    expect(await screen.findByText('Version 2.0')).toBeInTheDocument()
    expect(screen.queryByText('Version 1.0')).not.toBeInTheDocument()
  })
})

describe('RequireTermsAcceptance — revalidation window', () => {
  it('does not let the user accept a cached version while it is being revalidated', async () => {
    const v1 = { ...DOCUMENT, id: 'terms-1', version: '1.0', content: 'Old text.' }
    const v2 = { ...DOCUMENT, id: 'terms-2', version: '2.0', content: 'New text.' }
    server.use(
      http.get('http://localhost/api/v1/terms/current', async () => {
        await delay(50)
        return HttpResponse.json(v2)
      }),
    )
    const qc = new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: false, refetchOnWindowFocus: false } },
    })
    qc.setQueryData(['terms', 'current'], v1)
    const Wrapper = authWrapper([], [], null, { user: { terms_acceptance_required: true } })

    render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/']}>
            <Routes>
              <Route element={<RequireTermsAcceptance />}>
                <Route path="/" element={<p>the application</p>} />
              </Route>
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </Wrapper>,
    )

    // The stale copy is on screen, but accepting it is blocked until the revalidation lands.
    expect(await screen.findByText('Version 1.0')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I accept' })).toBeDisabled()

    expect(await screen.findByText('Version 2.0')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I accept' })).toBeEnabled()
  })
})
