import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth/auth-context'
import { ApiError } from '@/lib/api/problem'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { AuthShell } from './auth-shell'
import { OidcButtons } from './oidc-buttons'
import { useSignupEnabled } from '@/features/system-preferences/queries'

const schema = z.object({
  email: z.email('Enter a valid email'),
  password: z.string().min(1, 'Required'),
})
type FormValues = z.infer<typeof schema>

export function LoginPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const signupEnabled = useSignupEnabled()
  const [formError, setFormError] = useState<string | null>(null)

  const { register, handleSubmit, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '' },
  })

  if (auth.status === 'authenticated') {
    return <Navigate to="/" replace />
  }

  const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname
  const onSubmit = handleSubmit(async ({ email, password }) => {
    setFormError(null)
    try {
      await auth.login(email, password)
      navigate(from ?? '/', { replace: true })
    } catch (err) {
      setFormError(
        err instanceof ApiError && err.status === 401
          ? 'Invalid email or password.'
          : err instanceof Error
            ? err.message
            : 'Login failed.',
      )
    }
  })

  return (
    <AuthShell title="Sign in">
      <>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" autoComplete="email" {...register('email')} />
            {formState.errors.email && (
              <p className="text-destructive text-xs">{formState.errors.email.message}</p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              {...register('password')}
            />
            {formState.errors.password && (
              <p className="text-destructive text-xs">{formState.errors.password.message}</p>
            )}
          </div>
          {formError && <p className="text-destructive text-sm">{formError}</p>}
          <Button type="submit" className="w-full" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
        <div className="mt-4 space-y-1 text-center text-sm">
          <Link to="/password-reset" className="text-muted-foreground underline">
            Forgot password?
          </Link>
          {signupEnabled && (
            <p className="text-muted-foreground">
              No account?{' '}
              <Link to="/register" className="underline">
                Create one
              </Link>
            </p>
          )}
        </div>
        <OidcButtons />
      </>
    </AuthShell>
  )
}
