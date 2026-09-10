import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ConsentCard } from './consent-card'
import type { MeResponse, MeUpdate } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const DOCUMENT = {
  id: 'terms-1',
  version: '1.4',
  content: 'The rules.',
  published_at: '2026-09-02T12:00:00Z',
}
const ACCEPTED = { id: DOCUMENT.id, version: DOCUMENT.version, published_at: DOCUMENT.published_at }

function servesAccepted(document = DOCUMENT) {
  server.use(
    http.get(`http://localhost/api/v1/terms/${document.id}`, () => HttpResponse.json(document)),
  )
}

function renderCard(overrides: Partial<MeResponse> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const user: MeResponse = {
    id: 'u1',
    email: 'a@b.c',
    provider: 'local',
    email_verified: true,
    has_password: true,
    roles: [],
    permissions: [],
    organization: null,
    consent_terms: false,
    consent_emails: false,
    accepted_terms: null,
    terms_accepted_at: null,
    terms_acceptance_required: false,
    ...overrides,
  }
  const Wrapper = authWrapper([], [], null, { user })
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ConsentCard user={user} />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('ConsentCard', () => {
  it('reports the accepted version and when it was accepted', async () => {
    // A newer version is current — the card must still name the one this account agreed to, or it
    // states something false about a consent record.
    server.use(
      http.get('http://localhost/api/v1/terms/current', () =>
        HttpResponse.json({ ...DOCUMENT, id: 'terms-2', version: '2.0' }),
      ),
    )
    servesAccepted()

    renderCard({
      consent_terms: true,
      accepted_terms: ACCEPTED,
      terms_accepted_at: '2026-09-02T13:00:00Z',
    })

    expect(await screen.findByText(/Accepted version 1\.4 on /)).toBeInTheDocument()
    expect(screen.queryByText(/version 2\.0/)).not.toBeInTheDocument()
  })

  it('names no version when the accepted document is gone from the database', async () => {
    // `consent_terms` stays true while the projection is null — a version tombstoned by hand. The
    // acceptance is still a fact; the version is not knowable.
    server.use(http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(DOCUMENT)))

    renderCard({ consent_terms: true, terms_accepted_at: '2026-09-02T13:00:00Z' })

    expect(await screen.findByText(/^Accepted on /)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /read the terms of service/i })).toBeNull()
  })

  it('reports a failed read of the accepted document instead of dropping the link silently', async () => {
    server.use(
      http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(DOCUMENT)),
      http.get(
        `http://localhost/api/v1/terms/${DOCUMENT.id}`,
        () => new HttpResponse(null, { status: 500 }),
      ),
    )

    renderCard({ consent_terms: true, accepted_terms: ACCEPTED })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('offers no terms toggle — consent is granted against a version, not unticked', async () => {
    server.use(http.get('http://localhost/api/v1/terms/current', () => HttpResponse.json(DOCUMENT)))

    renderCard({ consent_terms: true })

    expect(await screen.findByText('Terms of service')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /terms of service/i })).not.toBeInTheDocument()
    // The email one is the only editable consent.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
  })

  it('says so when the platform has published nothing', async () => {
    renderCard()

    expect(await screen.findByText(/has not published terms of service/)).toBeInTheDocument()
  })

  it('PATCHes the email consent flag when toggled', async () => {
    let body: MeUpdate | null = null
    server.use(
      http.patch('http://localhost/api/v1/auth/me', async ({ request }) => {
        body = (await request.json()) as MeUpdate
        return HttpResponse.json({
          id: 'u1',
          email: 'a@b.c',
          provider: 'local',
          email_verified: true,
          has_password: true,
          roles: [],
          permissions: [],
          organization: null,
          consent_terms: false,
          consent_emails: true,
          terms_accepted_at: null,
          terms_acceptance_required: false,
        })
      }),
    )

    renderCard()
    await userEvent.click(screen.getByLabelText(/send me product email/i))

    await waitFor(() => expect(body).toEqual({ consent_emails: true }))
  })

  it('reports a failed toggle inline', async () => {
    server.use(
      http.patch('http://localhost/api/v1/auth/me', () => new HttpResponse(null, { status: 500 })),
    )

    renderCard()
    await userEvent.click(screen.getByLabelText(/send me product email/i))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('opens the accepted text, not whatever is current', async () => {
    server.use(
      http.get('http://localhost/api/v1/terms/current', () =>
        HttpResponse.json({ ...DOCUMENT, id: 'terms-2', version: '2.0', content: 'New rules.' }),
      ),
    )
    servesAccepted()

    renderCard({ consent_terms: true, accepted_terms: ACCEPTED })
    await userEvent.click(await screen.findByRole('button', { name: /read the terms of service/i }))

    expect(
      await screen.findByRole('region', { name: 'Terms of service, version 1.4' }),
    ).toHaveTextContent('The rules.')
  })

  it('does not claim nothing is published when the read failed', async () => {
    server.use(
      http.get(
        'http://localhost/api/v1/terms/current',
        () => new HttpResponse(null, { status: 500 }),
      ),
    )

    renderCard()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText(/has not published terms of service/)).not.toBeInTheDocument()
  })
})
