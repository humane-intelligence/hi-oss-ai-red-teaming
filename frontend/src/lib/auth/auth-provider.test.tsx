import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { apiClient } from '@/lib/api/client'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { queryClient } from '@/lib/query'
import { tokenStore } from './token-store'
import type { MeResponse } from '@/lib/api/types'
import { useAuth } from './auth-context'

function StatusProbe() {
  const auth = useAuth()
  return <div>status:{auth.status}</div>
}

function AdoptProbe() {
  const auth = useAuth()
  return (
    <>
      <button onClick={() => auth.adoptSession('jwt-from-idp').catch(() => {})}>adopt</button>
      <div>status:{auth.status}</div>
    </>
  )
}

function LogoutRaceProbe() {
  const auth = useAuth()
  // Settled-call count, like `GateProbe`'s: a client-side round trip completing *after* the held
  // read is released is the barrier these negative assertions need. Waiting on the msw handler
  // instead would only prove the handler resumed, not that the provider processed the response.
  const [calls, setCalls] = useState(0)
  return (
    <>
      <button
        onClick={() => void apiClient.GET('/api/v1/evaluations').then(() => setCalls((n) => n + 1))}
      >
        call
      </button>
      <button onClick={auth.logout}>sign out</button>
      <button onClick={() => void auth.adoptSession('other-jwt').catch(() => {})}>adopt</button>
      <div>status:{auth.status}</div>
      <div>email:{auth.user?.email ?? '-'}</div>
      <div>calls:{calls}</div>
    </>
  )
}

function GateProbe() {
  const auth = useAuth()
  // Settled-call count: a negative assertion about re-reads needs proof the refusals landed.
  const [calls, setCalls] = useState(0)
  return (
    <>
      <button
        onClick={() => void apiClient.GET('/api/v1/evaluations').then(() => setCalls((n) => n + 1))}
      >
        call
      </button>
      <div>gated:{auth.user?.terms_acceptance_required ? 'yes' : 'no'}</div>
      <div>calls:{calls}</div>
    </>
  )
}

// The `/auth/me` body the race tests vary the identity and the debt of. Annotated, so a field
// added or renamed in the contract fails here rather than serving a body the provider cannot use.
function meBody({ id = 'u1', email = 'a@b.c', owed = false } = {}): MeResponse {
  return {
    id,
    email,
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
    terms_acceptance_required: owed,
  }
}

const consentRefusal = () =>
  HttpResponse.json(
    { type: 'urn:redteam:error:terms-acceptance-required', status: 403 },
    { status: 403, headers: { 'content-type': 'application/problem+json' } },
  )

afterEach(() => {
  tokenStore.clear()
})

describe('AuthProvider', () => {
  it('fails closed instead of hanging on `loading` when /me rejects on mount', async () => {
    tokenStore.set('stale-jwt')
    server.use(http.get('http://localhost/api/v1/auth/me', () => HttpResponse.error()))

    renderWithProviders(<StatusProbe />)

    expect(await screen.findByText('status:unauthenticated')).toBeInTheDocument()
  })

  it("clears the previous identity's cached queries when adopting a new session", async () => {
    server.use(
      http.get('http://localhost/api/v1/auth/me', () =>
        HttpResponse.json({
          id: '1',
          email: 'ada@example.com',
          email_verified: true,
          provider: 'google',
        }),
      ),
    )
    queryClient.setQueryData(['probe'], 'whoever-was-here-last')

    renderWithProviders(<AdoptProbe />)
    await userEvent.click(screen.getByRole('button', { name: 'adopt' }))

    expect(await screen.findByText('status:authenticated')).toBeInTheDocument()
    expect(queryClient.getQueryData(['probe'])).toBeUndefined()
  })

  it('recovers from an already-authenticated state when a later adoption fails validation', async () => {
    // `/auth/callback` has no already-authenticated guard, so `adoptSession` can run
    // while `state` is still a previous `authenticated` session.
    server.use(
      http.get('http://localhost/api/v1/auth/me', () =>
        HttpResponse.json({
          id: '1',
          email: 'ada@example.com',
          email_verified: true,
          provider: 'google',
        }),
      ),
    )
    renderWithProviders(<AdoptProbe />)
    await userEvent.click(screen.getByRole('button', { name: 'adopt' }))
    expect(await screen.findByText('status:authenticated')).toBeInTheDocument()

    server.use(http.get('http://localhost/api/v1/auth/me', () => HttpResponse.error()))
    await userEvent.click(screen.getByRole('button', { name: 'adopt' }))

    expect(await screen.findByText('status:unauthenticated')).toBeInTheDocument()
  })

  it('re-reads the identity when a request is refused for an unaccepted version', async () => {
    // The backend enforces consent on every authenticated route; the identity lives in state, so
    // without this the gate would not appear until a reload.
    tokenStore.set('live-jwt')
    let reads = 0
    server.use(
      http.get('http://localhost/api/v1/auth/me', () => {
        reads += 1
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
          consent_emails: false,
          accepted_terms: null,
          terms_accepted_at: null,
          // The second read is the one that reports it; the first is the mount validation.
          terms_acceptance_required: reads > 1,
        })
      }),
      http.get('http://localhost/api/v1/evaluations', () =>
        HttpResponse.json(
          { type: 'urn:redteam:error:terms-acceptance-required', status: 403 },
          { status: 403, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )

    renderWithProviders(<GateProbe />)
    await screen.findByText('gated:no')

    await userEvent.click(screen.getByRole('button', { name: 'call' }))

    expect(await screen.findByText('gated:yes')).toBeInTheDocument()
  })

  it('does not resurrect the session when a sign-out lands during the consent re-read', async () => {
    // The re-read is in flight when the token goes away; adopting its response would leave an
    // authenticated state with no token, and the 401 path fires only while a token is held.
    tokenStore.set('live-jwt')
    const gate: { release: (() => void) | null } = { release: null }
    let reads = 0
    server.use(
      http.get('http://localhost/api/v1/auth/me', async () => {
        reads += 1
        // Hold the consent re-read — the mount read has to complete for the effect to register.
        if (reads === 2) await new Promise<void>((resolve) => (gate.release = resolve))
        return HttpResponse.json(meBody({ owed: reads >= 2 }))
      }),
      http.get('http://localhost/api/v1/evaluations', consentRefusal),
    )

    renderWithProviders(<LogoutRaceProbe />)
    await screen.findByText('status:authenticated')

    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    await screen.findByText('calls:1')
    await waitFor(() => expect(gate.release).not.toBeNull())
    await userEvent.click(screen.getByRole('button', { name: 'sign out' }))
    await screen.findByText('status:unauthenticated')
    gate.release?.()

    // A refusal settling client-side after the release. A high-water mark, not an ordering
    // guarantee: nothing formally sequences the released read's `.then` ahead of this fetch, but
    // the release happens first, and dropping the guard does fail this test.
    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    await screen.findByText('calls:2')

    expect(screen.getByText('status:unauthenticated')).toBeInTheDocument()
    expect(tokenStore.get()).toBeNull()
  })

  it('does not adopt the previous identity when a new session lands during the consent re-read', async () => {
    // The half a truthiness check would miss: after `adoptSession` the token store is populated
    // again, so `!tokenStore.get()` passes and the *old* account's in-flight read wins — writing
    // one identity under another's token. Guarded by comparing the token, not its presence.
    tokenStore.set('live-jwt')
    const gate: { release: (() => void) | null } = { release: null }
    let liveReads = 0
    server.use(
      http.get('http://localhost/api/v1/auth/me', async ({ request }) => {
        if (request.headers.get('Authorization') === 'Bearer other-jwt') {
          return HttpResponse.json(meBody({ id: 'u2', email: 'new@b.c' }))
        }
        liveReads += 1
        // Same: hold this token's consent re-read. Reads for the adopted token never reach here.
        if (liveReads === 2) await new Promise<void>((resolve) => (gate.release = resolve))
        return HttpResponse.json(meBody({ email: 'old@b.c', owed: liveReads >= 2 }))
      }),
      http.get('http://localhost/api/v1/evaluations', consentRefusal),
    )

    renderWithProviders(<LogoutRaceProbe />)
    await screen.findByText('email:old@b.c')

    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    await screen.findByText('calls:1')
    await waitFor(() => expect(gate.release).not.toBeNull())
    await userEvent.click(screen.getByRole('button', { name: 'adopt' }))
    await screen.findByText('email:new@b.c')
    gate.release?.()

    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    await screen.findByText('calls:2')

    expect(screen.getByText('email:new@b.c')).toBeInTheDocument()
  })

  it('stops re-reading once the held identity already owes an acceptance', async () => {
    // A provider above the router keeps polling behind the gate, so every poll is refused; one
    // re-read per refusal would be churn with nothing left to learn.
    tokenStore.set('live-jwt')
    let reads = 0
    server.use(
      http.get('http://localhost/api/v1/auth/me', () => {
        reads += 1
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
          consent_emails: false,
          accepted_terms: null,
          terms_accepted_at: null,
          terms_acceptance_required: true,
        })
      }),
      http.get('http://localhost/api/v1/evaluations', () =>
        HttpResponse.json(
          { type: 'urn:redteam:error:terms-acceptance-required', status: 403 },
          { status: 403, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )

    renderWithProviders(<GateProbe />)
    await screen.findByText('gated:yes')
    const afterMount = reads

    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    await userEvent.click(screen.getByRole('button', { name: 'call' }))
    // Both refusals have landed, so a re-read would have been counted by now.
    await screen.findByText('calls:2')

    expect(reads).toBe(afterMount)
  })
})
