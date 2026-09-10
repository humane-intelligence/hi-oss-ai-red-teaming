import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { AuthShell } from './auth-shell'
import { Button } from '@/components/ui/button'

export function VerifyEmailPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token')
  const [state, setState] = useState<'verifying' | 'ok' | 'error'>(token ? 'verifying' : 'error')

  useEffect(() => {
    if (!token) return
    let active = true
    apiClient
      .POST('/api/v1/auth/register/verify', { body: { token } })
      .then((r) => {
        if (!active) return
        unwrap(r)
        setState('ok')
      })
      .catch(() => {
        if (active) setState('error')
      })
    return () => {
      active = false
    }
  }, [token])

  const copy = {
    verifying: { title: 'Verifying…', subtitle: 'Confirming your email address.' },
    ok: { title: 'Email verified', subtitle: 'Your account is ready.' },
    error: { title: 'Verification failed', subtitle: 'This link is invalid or has expired.' },
  }[state]

  return (
    <AuthShell title={copy.title} subtitle={copy.subtitle}>
      {state !== 'verifying' && (
        <Button className="w-full" onClick={() => navigate('/login')}>
          Go to sign in
        </Button>
      )}
    </AuthShell>
  )
}
