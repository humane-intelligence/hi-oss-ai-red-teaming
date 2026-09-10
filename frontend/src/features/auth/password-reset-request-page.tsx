import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate } from 'react-router-dom'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { applyApiError } from '@/lib/api/form'
import { AuthShell } from './auth-shell'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

const schema = z.object({ email: z.email('Enter a valid email') })
type FormValues = z.infer<typeof schema>

export function PasswordResetRequestPage() {
  const navigate = useNavigate()
  const [done, setDone] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const { register, handleSubmit, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '' },
  })

  const onSubmit = handleSubmit(async ({ email }) => {
    setFormError(null)
    try {
      unwrap(await apiClient.POST('/api/v1/auth/password-resets/request', { body: { email } }))
      setDone(true)
    } catch (err) {
      if (!applyApiError(err, setError)) {
        setFormError(err instanceof Error ? err.message : 'Could not send reset link.')
      }
    }
  })

  if (done) {
    return (
      <AuthShell
        title="Check your email"
        subtitle="If an account exists for that email, we sent a reset link. Requests are rate-limited, so if you asked a moment ago, use the link from that earlier email."
      >
        <Button className="w-full" onClick={() => navigate('/login')}>
          Back to sign in
        </Button>
      </AuthShell>
    )
  }

  return (
    <AuthShell title="Reset password" subtitle="We'll email you a reset link.">
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Email" htmlFor="email" error={formState.errors.email?.message}>
          <Input id="email" type="email" autoComplete="email" {...register('email')} />
        </FormField>
        {formError && <p className="text-destructive text-sm">{formError}</p>}
        <Button type="submit" className="w-full" disabled={formState.isSubmitting}>
          {formState.isSubmitting ? 'Sending…' : 'Send reset link'}
        </Button>
      </form>
    </AuthShell>
  )
}
