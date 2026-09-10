import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth/auth-context'
import { Button } from '@/components/ui/button'
import { AuthShell } from './auth-shell'

// Codes the backend puts on `#error=` when a login cannot complete.
const ERROR_COPY: Record<string, string> = {
  // Google also emits `access_denied` for an org/admin block of the OAuth client, not
  // just a personal "cancel" on the consent screen — stay neutral on cause.
  access_denied: 'Your provider did not complete the sign-in.',
  email_unverified: 'Your provider has not verified this email address.',
  account_inactive: 'This account cannot sign in. Ask an administrator to reactivate it.',
  invalid_claims: 'Your provider did not return the details we need to sign you in.',
  // Same copy as the register page's invite-only note — one message for one policy.
  invite_only: 'Registration is invite-only. Ask an administrator for an invitation.',
}
const FALLBACK_ERROR = 'Sign-in did not complete. Please try again.'

type Outcome = { session: string } | { error: string }

function parseFragment(hash: string): Outcome {
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const errorCode = params.get('error')
  if (errorCode) return { error: ERROR_COPY[errorCode] ?? FALLBACK_ERROR }
  const accessToken = params.get('access_token')
  return accessToken ? { session: accessToken } : { error: FALLBACK_ERROR }
}

export function OidcCallbackPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const { hash } = useLocation()
  // Read once at mount — the effect strips the fragment, so a later read is empty.
  const [outcome] = useState(() => parseFragment(hash))
  const [failed, setFailed] = useState(false)
  // `auth` changes identity as its state settles; the handshake runs once.
  const started = useRef(false)
  // Re-armed unconditionally in the effect (not gated behind `started`) to survive
  // StrictMode's mount/cleanup/remount — see `write-note-dialog.tsx`'s `reachable`.
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    if (!started.current && 'session' in outcome) {
      started.current = true

      // Drop the token from the URL before anything can read or share it, and
      // replace rather than push so Back can't return to a URL holding it. Pass the
      // existing history state through rather than `null` — React Router's `idx`
      // bookkeeping lives there, and clobbering it corrupts POP-delta calculations.
      window.history.replaceState(
        window.history.state,
        '',
        `${window.location.pathname}${window.location.search}`,
      )

      auth
        .adoptSession(outcome.session)
        .then(() => {
          if (live.current) navigate('/', { replace: true })
        })
        .catch(() => {
          if (live.current) setFailed(true)
        })
    }
    return () => {
      live.current = false
    }
  }, [outcome, auth, navigate])

  const message = 'error' in outcome ? outcome.error : failed ? FALLBACK_ERROR : null
  const backToSignIn = useRef<HTMLButtonElement>(null)

  // Move focus onto the recovery action once it appears — otherwise a keyboard/screen-reader
  // user starts back at the top of the page with nothing else focusable.
  useEffect(() => {
    if (message) backToSignIn.current?.focus()
  }, [message])

  if (!message) {
    return <AuthShell title="Signing in…" subtitle="Completing the handshake with your provider." />
  }

  return (
    <AuthShell title="Sign-in failed" subtitle={message}>
      <Button
        ref={backToSignIn}
        aria-describedby="auth-shell-subtitle"
        className="w-full"
        onClick={() => navigate('/login', { replace: true })}
      >
        Back to sign in
      </Button>
    </AuthShell>
  )
}
