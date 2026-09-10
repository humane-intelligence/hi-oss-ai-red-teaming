import { useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { applyApiError } from '@/lib/api/form'
import { ApiError } from '@/lib/api/problem'
import { AuthShell } from './auth-shell'
import { PASSWORD_REQUIREMENTS_ID, passwordSchema } from './password-policy'
import { PasswordRequirements } from './password-requirements'
import { usePasswordPolicy } from '@/features/system-preferences/queries'
import { useCurrentTerms } from '@/features/terms/queries'
import { TermsDialogLink } from '@/features/terms/terms-dialog'
import { ConsentCheckbox } from '@/features/terms/consent-checkbox'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

// `termsRequired` as a refinement, not a `z.literal(true)` field, so the inferred form shape does
// not change with the published state — same call as the register form.
const buildSchema = (policy: PublicPasswordPolicyResponse, termsRequired: boolean) =>
  z
    .object({
      password: passwordSchema(policy),
      first_name: z.string().optional(),
      last_name: z.string().optional(),
      consent_terms: z.boolean(),
      consent_emails: z.boolean(),
    })
    .refine((values) => !termsRequired || values.consent_terms, {
      path: ['consent_terms'],
      message: 'You must accept the terms of service to continue',
    })
type FormValues = z.infer<ReturnType<typeof buildSchema>>

// The fields this form renders, so an errors[] entry aimed at one it does not render cannot count
// as mapped and vanish.
const INVITE_FIELDS = ['password', 'first_name', 'last_name', 'consent_terms'] as const

export function AcceptInvitePage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token') ?? ''
  const [formError, setFormError] = useState<string | null>(null)

  const preview = useQuery({
    queryKey: ['invitation', token],
    enabled: token.length > 0,
    retry: false,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/auth/invitations/accept', {
          params: { query: { token } },
        }),
      ),
  })

  const { policy, isPending: policyPending } = usePasswordPolicy()
  const terms = useCurrentTerms()
  const schema = useMemo(() => buildSchema(policy, Boolean(terms.data)), [policy, terms.data])
  const { register, handleSubmit, setError, setValue, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      password: '',
      first_name: '',
      last_name: '',
      consent_terms: false,
      consent_emails: false,
    },
  })

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null)
    try {
      unwrap(
        await apiClient.POST('/api/v1/auth/invitations/accept', {
          body: {
            token,
            password: values.password,
            first_name: values.first_name || undefined,
            last_name: values.last_name || undefined,
            consent_terms: values.consent_terms,
            consent_emails: values.consent_emails,
            terms_id: terms.data?.id,
          },
        }),
      )
      navigate('/login')
    } catch (err) {
      // Same recovery as the register form: a 409 means the version moved under this form, so
      // re-read it and clear the tick rather than looping on a stale id.
      if (err instanceof ApiError && err.status === 409) {
        await terms.refetch()
        setValue('consent_terms', false)
        setFormError('The terms of service changed. Please review and accept them again.')
        return
      }
      if (!applyApiError(err, setError, INVITE_FIELDS)) {
        setFormError(err instanceof Error ? err.message : 'Could not accept invitation.')
      }
    }
  })

  if (!token || preview.isError) {
    return (
      <AuthShell
        title="Invitation invalid"
        subtitle="This invitation link is invalid or has expired."
      >
        <Button className="w-full" onClick={() => navigate('/login')}>
          Back to sign in
        </Button>
      </AuthShell>
    )
  }

  if (preview.isLoading || !preview.data) {
    return (
      <AuthShell title="Loading invitation…" subtitle="One moment.">
        <></>
      </AuthShell>
    )
  }

  const { email, inviter_name, role_names } = preview.data

  return (
    <AuthShell
      title="Accept invitation"
      subtitle={
        inviter_name
          ? `${inviter_name} invited you to RED·TEAM.`
          : 'You have been invited to RED·TEAM.'
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Email" htmlFor="email">
          <Input id="email" value={email} readOnly />
        </FormField>
        {role_names.length > 0 && (
          <p className="text-muted-foreground text-xs">You'll join as {role_names.join(', ')}.</p>
        )}
        <FormField label="Password" htmlFor="password" error={formState.errors.password?.message}>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            aria-describedby={PASSWORD_REQUIREMENTS_ID}
            {...register('password')}
          />
        </FormField>
        <PasswordRequirements policy={policy} pending={policyPending} />
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
        {terms.isPending && (
          // The submit is disabled for this window, so it has to say why — the sibling error path does,
          // and the gate renders the same kind of line.
          <p className="text-muted-foreground text-sm">Checking the terms of service…</p>
        )}
        {terms.isError && (
          // The terms read owns its errors (no global toast), and without a version the form cannot
          // collect consent at all — so say so and block the submit rather than posting a signup
          // the backend will refuse with a field error this form is not rendering.
          <p role="alert" className="text-destructive text-sm">
            Could not load the terms of service, which you have to accept to continue.{' '}
            <button type="button" onClick={() => void terms.refetch()} className="underline">
              Try again
            </button>
          </p>
        )}
        {terms.data && (
          <ConsentCheckbox
            id="consent_terms"
            label="I accept the terms of service"
            hint={`Version ${terms.data.version} — required to continue.`}
            error={formState.errors.consent_terms?.message}
            extra={<TermsDialogLink terms={terms.data} />}
            {...register('consent_terms')}
          />
        )}
        <ConsentCheckbox
          id="consent_emails"
          label="Send me product email"
          hint="Optional. Account email — verification, password resets — is sent either way."
          {...register('consent_emails')}
        />
        {formError && <p className="text-destructive text-sm">{formError}</p>}
        <Button
          type="submit"
          className="w-full"
          disabled={formState.isSubmitting || terms.isPending || terms.isError}
        >
          {formState.isSubmitting ? 'Setting up…' : 'Accept & set password'}
        </Button>
      </form>
    </AuthShell>
  )
}
