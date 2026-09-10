import { useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { useAuth } from '@/lib/auth/auth-context'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { useReachable } from '@/lib/use-reachable'
import { PASSWORD_REQUIREMENTS_ID, passwordSchema } from '@/features/auth/password-policy'
import { PasswordRequirements } from '@/features/auth/password-requirements'
import { usePasswordPolicy } from '@/features/system-preferences/queries'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ConsentCard } from './consent-card'
import { useUpdateMe } from './mutations'
import type { MeResponse, PublicPasswordPolicyResponse } from '@/lib/api/types'

export function AccountPage() {
  const { user } = useAuth()
  // RequireAuth guards the route; the check narrows the type for the cards below.
  if (!user) return null
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader title="Account settings" description={user.email} />
      <ProfileCard user={user} />
      {user.has_password ? (
        <PasswordCard email={user.email} />
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Password</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">
              This account has no local password — sign-in is managed by your identity provider.
            </p>
          </CardContent>
        </Card>
      )}
      <ConsentCard user={user} />
    </div>
  )
}

const profileSchema = z.object({
  first_name: z.string().trim().max(255, 'At most 255 characters'),
  last_name: z.string().trim().max(255, 'At most 255 characters'),
})
type ProfileFormValues = z.infer<typeof profileSchema>
const PROFILE_FIELDS = ['first_name', 'last_name'] as const

function ProfileCard({ user }: { user: MeResponse }) {
  const update = useUpdateMe()
  const reachable = useReachable()
  const [formError, setFormError] = useState<string | null>(null)
  const { register, handleSubmit, setError, reset, formState } = useForm<ProfileFormValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: { first_name: user.first_name ?? '', last_name: user.last_name ?? '' },
  })
  useUnsavedGuard(formState.isDirty)

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null)
    try {
      const me = await update.mutateAsync({
        first_name: values.first_name || null,
        last_name: values.last_name || null,
      })
      // Re-seed so the form is pristine against what actually saved (trimmed values).
      reset({ first_name: me.first_name ?? '', last_name: me.last_name ?? '' })
    } catch (err) {
      // The mutation opts out of the global toast, so anything not mapped onto an owned
      // field has to surface here — or as a toast, if this card is already gone.
      if (!reachable.current) toast.error(humanizeError(err))
      else if (!applyApiError(err, setError, PROFILE_FIELDS)) {
        setFormError(humanizeError(err))
      }
    }
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Profile</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="grid grid-cols-2 gap-4">
            <FormField
              label="First name"
              htmlFor="first_name"
              error={formState.errors.first_name?.message}
            >
              <Input id="first_name" autoComplete="given-name" {...register('first_name')} />
            </FormField>
            <FormField
              label="Last name"
              htmlFor="last_name"
              error={formState.errors.last_name?.message}
            >
              <Input id="last_name" autoComplete="family-name" {...register('last_name')} />
            </FormField>
          </div>
          {/* `role="alert"`: the mutation opts out of the global toast, so a failed save
              is announced here or nowhere. */}
          {formError && (
            <p role="alert" className="text-destructive text-sm">
              {formError}
            </p>
          )}
          <div className="flex justify-end">
            <Button type="submit" disabled={!formState.isDirty || formState.isSubmitting}>
              {formState.isSubmitting ? 'Saving…' : 'Save changes'}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

const buildSchema = (policy: PublicPasswordPolicyResponse) =>
  z
    .object({
      current_password: z.string().min(1, 'Enter your current password'),
      password: passwordSchema(policy),
      confirm_password: z.string(),
    })
    .refine((values) => values.password === values.confirm_password, {
      message: 'Passwords do not match',
      path: ['confirm_password'],
    })
type PasswordFormValues = z.infer<ReturnType<typeof buildSchema>>
const PASSWORD_FIELDS = ['current_password', 'password'] as const

function PasswordCard({ email }: { email: string }) {
  const { logout } = useAuth()
  const reachable = useReachable()
  const [formError, setFormError] = useState<string | null>(null)
  const { policy, isPending: policyPending } = usePasswordPolicy()
  const schema = useMemo(() => buildSchema(policy), [policy])
  const { register, handleSubmit, setError, formState } = useForm<PasswordFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: '', password: '', confirm_password: '' },
  })

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null)
    try {
      unwrap(
        await apiClient.POST('/api/v1/auth/me/password', {
          body: { current_password: values.current_password, password: values.password },
        }),
      )
      // The change revoked every session, this one included — drop the dead
      // token and let RequireAuth land the user on the login page.
      toast.success('Password changed', { description: 'Sign in with your new password.' })
      logout()
    } catch (err) {
      if (!reachable.current) toast.error(humanizeError(err))
      else if (!applyApiError(err, setError, PASSWORD_FIELDS)) {
        setFormError(humanizeError(err))
      }
    }
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Password</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <p className="text-muted-foreground text-sm">
            Changing the password signs you out everywhere, this session included — you sign back in
            with the new one.
          </p>
          {/* Password managers need a username input in the form to update the stored
              credential — sr-only rather than `hidden`, which some managers skip. */}
          <input
            type="text"
            name="username"
            autoComplete="username"
            value={email}
            readOnly
            aria-hidden="true"
            tabIndex={-1}
            className="sr-only"
          />
          <FormField
            label="Current password"
            htmlFor="current_password"
            error={formState.errors.current_password?.message}
          >
            <Input
              id="current_password"
              type="password"
              autoComplete="current-password"
              {...register('current_password')}
            />
          </FormField>
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
          <FormField
            label="Confirm new password"
            htmlFor="confirm_password"
            error={formState.errors.confirm_password?.message}
          >
            <Input
              id="confirm_password"
              type="password"
              autoComplete="new-password"
              {...register('confirm_password')}
            />
          </FormField>
          <PasswordRequirements policy={policy} pending={policyPending} />
          {formError && (
            <p role="alert" className="text-destructive text-sm">
              {formError}
            </p>
          )}
          <div className="flex justify-end">
            <Button type="submit" disabled={formState.isSubmitting}>
              {formState.isSubmitting ? 'Saving…' : 'Change password'}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
