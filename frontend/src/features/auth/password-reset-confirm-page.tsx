import { useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { applyApiError } from '@/lib/api/form'
import { AuthShell } from './auth-shell'
import { PASSWORD_REQUIREMENTS_ID, passwordSchema } from './password-policy'
import { PasswordRequirements } from './password-requirements'
import { usePasswordPolicy } from '@/features/system-preferences/queries'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

const buildSchema = (policy: PublicPasswordPolicyResponse) =>
  z.object({ password: passwordSchema(policy) })
type FormValues = z.infer<ReturnType<typeof buildSchema>>

export function PasswordResetConfirmPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token')
  const [formError, setFormError] = useState<string | null>(null)
  const { policy, isPending: policyPending } = usePasswordPolicy()
  const schema = useMemo(() => buildSchema(policy), [policy])
  const { register, handleSubmit, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { password: '' },
  })

  if (!token) {
    return (
      <AuthShell title="Invalid reset link" subtitle="This link is missing or malformed.">
        <Button className="w-full" onClick={() => navigate('/login')}>
          Back to sign in
        </Button>
      </AuthShell>
    )
  }

  const onSubmit = handleSubmit(async ({ password }) => {
    setFormError(null)
    try {
      unwrap(
        await apiClient.POST('/api/v1/auth/password-resets/confirm', {
          body: { token, password },
        }),
      )
      navigate('/login')
    } catch (err) {
      if (!applyApiError(err, setError)) {
        setFormError(err instanceof Error ? err.message : 'Could not reset password.')
      }
    }
  })

  return (
    <AuthShell title="Set a new password" subtitle="Choose a new password for your account.">
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField
          label="New password"
          htmlFor="password"
          error={formState.errors.password?.message}
        >
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            aria-describedby={PASSWORD_REQUIREMENTS_ID}
            {...register('password')}
          />
        </FormField>
        <PasswordRequirements policy={policy} pending={policyPending} />
        {formError && <p className="text-destructive text-sm">{formError}</p>}
        <Button type="submit" className="w-full" disabled={formState.isSubmitting}>
          {formState.isSubmitting ? 'Saving…' : 'Update password'}
        </Button>
      </form>
    </AuthShell>
  )
}
