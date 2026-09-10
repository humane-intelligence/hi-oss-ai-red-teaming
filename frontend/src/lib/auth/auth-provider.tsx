import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { apiClient, CONSENT_REQUIRED_EVENT, UNAUTHORIZED_EVENT } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { queryClient } from '@/lib/query'
import { tokenStore } from './token-store'
import { AuthContext, type AuthContextValue, type AuthState } from './auth-context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(() =>
    tokenStore.get()
      ? { status: 'loading', user: null }
      : { status: 'unauthenticated', user: null },
  )

  // Validate an existing token once on mount.
  useEffect(() => {
    if (!tokenStore.get()) return
    let active = true
    apiClient
      .GET('/api/v1/auth/me')
      .then(({ data }) => {
        if (!active) return
        setState(
          data
            ? { status: 'authenticated', user: data }
            : { status: 'unauthenticated', user: null },
        )
      })
      .catch(() => {
        // openapi-fetch resolves `{data: undefined, error}` for a completed response
        // (the `.then()` above already handles a 500 that way) — only a genuine
        // transport failure (network drop) rejects. Fail closed the same way rather
        // than leaving an unhandled rejection and the state stuck on `loading`.
        if (active) setState({ status: 'unauthenticated', user: null })
      })
    return () => {
      active = false
    }
  }, [])

  // Token expired/revoked mid-session: the client cleared it and fired this event.
  useEffect(() => {
    const onUnauthorized = () => setState({ status: 'unauthenticated', user: null })
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  // A version was published mid-session and the backend is refusing this account. The identity is
  // held in state rather than a query, so nothing else would notice until a reload — this re-reads
  // it and the acceptance gate takes over. `GET /me` stays exempt from the refusal, so it answers.
  const gated = state.user?.terms_acceptance_required ?? false
  useEffect(() => {
    // Every refused request fires the event, and a provider above the router keeps polling behind
    // the gate: once the held identity already says an acceptance is owed, there is nothing left
    // to learn, and a re-read per poll would be pure churn.
    if (gated) return
    let active = true
    let reading = false
    const onConsentRequired = () => {
      const token = tokenStore.get()
      if (reading || !token) return
      reading = true
      apiClient
        .GET('/api/v1/auth/me')
        .then(({ data }) => {
          // A sign-out — or a different account signing in — can land while this is in flight.
          // Adopting the response then would restore an identity the held token no longer backs,
          // and nothing recovers that: the 401 path fires only while a token is held. Compared by
          // identity rather than truthiness, so a new session is caught as well as none.
          if (!active || tokenStore.get() !== token || !data) return
          setState({ status: 'authenticated', user: data })
        })
        // Best effort: the next refusal fires this again, and the 401 path owns a dead session.
        .catch(() => {})
        .finally(() => {
          reading = false
        })
    }
    window.addEventListener(CONSENT_REQUIRED_EVENT, onConsentRequired)
    return () => {
      active = false
      window.removeEventListener(CONSENT_REQUIRED_EVENT, onConsentRequired)
    }
  }, [gated])

  const adopt = useCallback(async (accessToken: string) => {
    tokenStore.set(accessToken)
    try {
      const user = unwrap(await apiClient.GET('/api/v1/auth/me'))
      // A session swap must not leave the previous identity's cached queries
      // behind — otherwise a different user completing this flow on the same
      // tab briefly renders whoever was here last, until each query refetches.
      queryClient.clear()
      setState({ status: 'authenticated', user })
    } catch (error) {
      // Reset state, not just the token: `/auth/callback` has no already-authenticated
      // guard, so this can run while `state` is still yesterday's `authenticated` — a
      // token-only clear would leave that stale state in place with no token to back
      // it, and (unlike a 401) nothing left to fire `UNAUTHORIZED_EVENT` and recover it.
      tokenStore.clear()
      setState({ status: 'unauthenticated', user: null })
      throw error
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      login: async (email, password) => {
        const tokens = unwrap(
          await apiClient.POST('/api/v1/auth/login', { body: { email, password } }),
        )
        await adopt(tokens.access_token)
      },
      adoptSession: adopt,
      logout: () => {
        tokenStore.clear()
        queryClient.clear()
        setState({ status: 'unauthenticated', user: null })
      },
      updateUser: (user) =>
        setState((prev) =>
          prev.status === 'authenticated' ? { status: 'authenticated', user } : prev,
        ),
    }),
    [state, adopt],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
