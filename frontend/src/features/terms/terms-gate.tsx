import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { useAuth } from '@/lib/auth/auth-context'
import { ApiError, humanizeError } from '@/lib/api/problem'
import { Button } from '@/components/ui/button'
import { useCurrentTerms } from './queries'
import { useAcceptTerms } from './mutations'
import { TermsText } from './terms-text'

// Route guard between `RequireAuth` and the app shell: an account whose consent lags gets the gate
// *instead of* the application, not a modal floating over it — no route below it renders, so no
// query for data the user has not yet accepted terms to see ever fires, and there is no dismissal
// to get wrong. Signing out is the only other way past it. (Providers mounted *above* the router
// keep running: an export job already being tracked goes on polling behind the gate.)
export function RequireTermsAcceptance() {
  const { user } = useAuth()
  if (!user?.terms_acceptance_required) return <Outlet />
  return <TermsGate />
}

export function TermsGate() {
  const { user, logout } = useAuth()
  const terms = useCurrentTerms()
  const accept = useAcceptTerms()
  const [error, setError] = useState<string | null>(null)

  // The flag said acceptance is required, so a `null` here means the version vanished between that
  // read and this one (only reachable by deleting the row straight in the database). Nothing to
  // consent to, so let the app through rather than trap the account behind an empty gate.
  if (terms.data === null) return <Outlet />

  const onAccept = async () => {
    if (!terms.data) return
    setError(null)
    try {
      await accept.mutateAsync(terms.data.id)
    } catch (err) {
      // A 409 means a newer version landed while this one was on screen; the refetch swaps the text
      // and the user accepts what they can actually see. Same sentence as the two onboarding
      // forms — one event, one voice, and the backend's own detail names a version the reader
      // never saw.
      if (err instanceof ApiError && err.status === 409) {
        await terms.refetch()
        setError('The terms of service changed. Please review and accept them again.')
        return
      }
      setError(humanizeError(err))
    }
  }

  return (
    <div className="grid min-h-svh place-items-center p-6">
      <div className="w-full max-w-2xl space-y-6">
        {/* `role="status"` (implicit aria-live="polite") — this screen replaces the whole app
            under the reader with no navigation of their own, so it announces itself. Same idiom as
            `auth-shell.tsx`'s header. */}
        <header role="status">
          <h1 className="font-display text-2xl font-semibold tracking-tight">Terms of service</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            {user?.consent_terms
              ? 'The terms have changed since you last accepted them. Review and accept to continue.'
              : 'Review and accept the terms to continue.'}
            {user?.email && <> Signed in as {user.email}.</>}
          </p>
        </header>

        {terms.isPending && <p className="text-muted-foreground text-sm">Loading the terms…</p>}
        {terms.isError && (
          // Fail closed: the account owes an acceptance, so a failed read offers a retry, never a
          // way around.
          <p role="alert" className="text-destructive text-sm">
            {humanizeError(terms.error)}
          </p>
        )}
        {terms.data && (
          <>
            <p className="text-muted-foreground text-xs">Version {terms.data.version}</p>
            <TermsText
              content={terms.data.content}
              label={`Terms of service, version ${terms.data.version}`}
            />
          </>
        )}

        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}

        <div className="flex items-center justify-between gap-4">
          {/* Never disabled: it is the only other way past this screen, and an accept that hangs
              would otherwise strand the account here. */}
          <Button variant="ghost" onClick={logout}>
            Sign out
          </Button>
          <div className="flex gap-2">
            {terms.isError && (
              <Button variant="outline" onClick={() => terms.refetch()}>
                Try again
              </Button>
            )}
            {/* `isFetching`, not just `isPending`: a cached document is rendered while the
                revalidation is still in flight, and accepting in that window would submit the id
                of a version that may already be superseded. */}
            <Button
              onClick={onAccept}
              disabled={!terms.data || terms.isFetching || accept.isPending}
            >
              {accept.isPending ? 'Accepting…' : 'I accept'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
