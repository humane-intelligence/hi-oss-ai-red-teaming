import { useEffect, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth/auth-context'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { applyApiError } from '@/lib/api/form'
import { ApiError } from '@/lib/api/problem'
import { AuthShell } from './auth-shell'
import { useResendVerification } from './mutations'
import { usePasswordPolicy, useSignupEnabled } from '@/features/system-preferences/queries'
import { useCurrentTerms } from '@/features/terms/queries'
import { TermsDialogLink } from '@/features/terms/terms-dialog'
import { ConsentCheckbox } from '@/features/terms/consent-checkbox'
import { PASSWORD_REQUIREMENTS_ID, passwordSchema } from './password-policy'
import { PasswordRequirements } from './password-requirements'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

// `termsRequired` is a refinement rather than a `z.literal(true)` field so the inferred form shape
// stays the same whether or not a version is published — one `FormValues`, no union.
const buildSchema = (policy: PublicPasswordPolicyResponse, termsRequired: boolean) =>
  z
    .object({
      email: z.email('Enter a valid email'),
      password: passwordSchema(policy),
      first_name: z.string().optional(),
      last_name: z.string().optional(),
      consent_terms: z.boolean(),
      consent_emails: z.boolean(),
    })
    .refine((values) => !termsRequired || values.consent_terms, {
      path: ['consent_terms'],
      message: 'You must accept the terms of service to create an account',
    })
// Derived, not hand-written: a hand-written shape lets a future field ship unvalidated, since
// the compiler accepts a FormValues key the schema never mentions.
type FormValues = z.infer<ReturnType<typeof buildSchema>>

// The fields this form renders and can therefore show an inline error on. Without the whitelist a
// `consent_terms` error would count as mapped and vanish whenever the checkbox is not rendered.
const SIGNUP_FIELDS = ['email', 'password', 'first_name', 'last_name', 'consent_terms'] as const

// Each resend revokes the link already in the recipient's inbox, so an immediately
// re-enabled button turns "did anything happen?" into killing the mail in flight.
export const RESEND_COOLDOWN_SECONDS = 30

export function RegisterPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const signupEnabled = useSignupEnabled()
  const resend = useResendVerification()
  const [done, setDone] = useState(false)
  const [registeredEmail, setRegisteredEmail] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [resendReadyAt, setResendReadyAt] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const { policy, isPending: policyPending } = usePasswordPolicy()
  const terms = useCurrentTerms()
  const schema = useMemo(() => buildSchema(policy, Boolean(terms.data)), [policy, terms.data])
  const { register, handleSubmit, setError, setValue, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      email: '',
      password: '',
      first_name: '',
      last_name: '',
      consent_terms: false,
      consent_emails: false,
    },
  })

  // Off a wall-clock deadline, not a tick count: this screen is the one the user leaves
  // to go read their inbox, and a backgrounded tab's timers are throttled arbitrarily.
  const resendCooldown = Math.max(0, Math.ceil((resendReadyAt - now) / 1000))
  useEffect(() => {
    if (resendCooldown === 0) return
    const timer = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(timer)
  }, [resendCooldown])

  if (auth.status === 'authenticated') return <Navigate to="/" replace />

  // Ahead of the invite-only gate: a refetch on reconnect can flip the flag while this is up, and
  // replacing a submitted registration's confirmation with "ask for an invitation" would read as
  // the account never having been created — resend serves exactly those in-flight signups.
  if (done) {
    return (
      <AuthShell title="Check your email" subtitle="We sent a link to verify your account.">
        <div className="space-y-2">
          <Button className="w-full" onClick={() => navigate('/login')}>
            Back to sign in
          </Button>
          <Button
            variant="ghost"
            className="w-full"
            disabled={resend.isPending || resendCooldown > 0}
            onClick={() =>
              resend.mutate(registeredEmail, {
                onSuccess: () => {
                  setNow(Date.now())
                  setResendReadyAt(Date.now() + RESEND_COOLDOWN_SECONDS * 1000)
                },
              })
            }
          >
            {resend.isPending
              ? 'Sending…'
              : resendCooldown > 0
                ? `Resend available in ${resendCooldown}s`
                : "Didn't get it? Resend link"}
          </Button>
        </div>
      </AuthShell>
    )
  }

  if (!signupEnabled) {
    return (
      <AuthShell
        title="Registration is invite-only"
        subtitle="Open sign-up is disabled on this platform. Ask an administrator for an invitation."
      >
        <Button className="w-full" onClick={() => navigate('/login')}>
          Back to sign in
        </Button>
      </AuthShell>
    )
  }

  const onSubmit = handleSubmit(async (values) => {
    try {
      setFormError(null)
      unwrap(
        await apiClient.POST('/api/v1/auth/register', {
          body: {
            email: values.email,
            password: values.password,
            first_name: values.first_name || undefined,
            last_name: values.last_name || undefined,
            consent_terms: values.consent_terms,
            consent_emails: values.consent_emails,
            // Names the version actually rendered — the backend refuses anything else rather than
            // recording consent against text this form never showed.
            terms_id: terms.data?.id,
          },
        }),
      )
      setRegisteredEmail(values.email)
      setDone(true)
    } catch (err) {
      // A 409 means a version landed between this form's read and the submit. Re-read it and clear
      // the tick: resubmitting the same id would loop, and carrying the tick over to text the user
      // has not seen is the one thing consent must never do.
      if (err instanceof ApiError && err.status === 409) {
        await terms.refetch()
        setValue('consent_terms', false)
        setFormError('The terms of service changed. Please review and accept them again.')
        return
      }
      if (!applyApiError(err, setError, SIGNUP_FIELDS)) {
        setFormError(err instanceof Error ? err.message : 'Could not create account.')
      }
    }
  })

  return (
    <AuthShell title="Create account">
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Email" htmlFor="email" error={formState.errors.email?.message}>
          <Input id="email" type="email" autoComplete="email" {...register('email')} />
        </FormField>
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
          // The submit is disabled for this window, so it has to say why — the sibling error path
          // does, and the gate renders the same kind of line.
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
          {formState.isSubmitting ? 'Creating…' : 'Create account'}
        </Button>
        <p className="text-muted-foreground text-center text-sm">
          Already have an account?{' '}
          <Link to="/login" className="underline">
            Sign in
          </Link>
        </p>
      </form>
    </AuthShell>
  )
}
