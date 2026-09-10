import { useLayoutEffect } from 'react'
import type { ReactNode } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { usePlatformSettings } from './queries'
import { useUpdatePlatformSettings } from './mutations'
import { LicensePicker } from '@/features/licenses/license-picker'
import { useLicenses } from '@/features/licenses/queries'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { usePermissions } from '@/lib/auth/use-permissions'
import { cn } from '@/lib/utils'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { PageHeader } from '@/components/shared/page-header'
import { TermsSettingsSection } from '@/features/terms/terms-settings-section'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import type { PlatformSettingsUpdate } from '@/lib/api/types'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from '@/components/ui/field'

const schema = z.object({
  default_license_id: z.string().min(1, 'Required'),
  invite_only: z.boolean(),
  email_verification_ttl_hours: z
    .number({ message: 'Enter a number of hours' })
    .int('Whole hours only')
    .min(1, 'At least 1 hour')
    .max(8760, 'At most 8760 hours (one year)'),
  // The floor is the contract's own minimum — the API rejects anything lower, so offering it
  // here would only produce a 422 the operator can't act on.
  password_min_length: z
    .number({ message: 'Enter a number of characters' })
    .int('Whole characters only')
    .min(8, 'At least 8 characters')
    .max(128, 'At most 128 characters'),
  password_require_uppercase: z.boolean(),
  password_require_digit: z.boolean(),
  password_require_symbol: z.boolean(),
  password_reset_cooldown_seconds: z
    .number({ message: 'Enter a number of seconds' })
    .int('Whole seconds only')
    .min(0, 'At least 0 seconds')
    .max(3600, 'At most 3600 seconds (one hour)'),
  password_reset_max_per_day: z
    .number({ message: 'Enter a number of links' })
    .int('Whole links only')
    .min(1, 'At least 1 link')
    .max(100, 'At most 100 links'),
})
type FormValues = z.infer<typeof schema>

// One card per settings domain — a new knob lands as a row in its section (or a new section),
// never by widening a single flat form.
function SettingsSection({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <p className="text-muted-foreground text-sm">{description}</p>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

// Platform-wide settings singleton: always an "edit" of the one row — no create/list, so no
// breadcrumbs/back. Read-only without `platform_settings:update` (route needs only `:read`).
export function SystemPreferencesPage() {
  const settings = usePlatformSettings()
  // The picker fetches the same query itself; reading it here too (shared cache) lets the form
  // mount only once the options exist — an uncontrolled select can't hold a value whose option
  // hasn't rendered yet, so resetting earlier would leave it displaying the first option.
  const licenses = useLicenses()
  const update = useUpdatePlatformSettings()
  const { has } = usePermissions()
  const canUpdate = has('platform_settings:update')
  // Either read failing takes the page to its error state — a skeleton next to it would promise
  // content that is not coming.
  const failed = settings.isError || licenses.isError

  const { register, handleSubmit, control, reset, setError, setValue, formState } =
    useForm<FormValues>({
      resolver: zodResolver(schema),
      defaultValues: {
        default_license_id: '',
        invite_only: false,
        email_verification_ttl_hours: 24,
        password_min_length: 8,
        password_require_uppercase: false,
        password_require_digit: false,
        password_require_symbol: false,
        password_reset_cooldown_seconds: 60,
        password_reset_max_per_day: 5,
      },
    })
  const defaultLicenseId = useWatch({ control, name: 'default_license_id' })
  const inviteOnly = useWatch({ control, name: 'invite_only' })
  const requiredChars = {
    password_require_uppercase: useWatch({ control, name: 'password_require_uppercase' }),
    password_require_digit: useWatch({ control, name: 'password_require_digit' }),
    password_require_symbol: useWatch({ control, name: 'password_require_symbol' }),
  }

  // Read during render, not inside the submit callback: react-hook-form only maintains the state
  // slices something has subscribed to, and the subscription is what the property read registers.
  const { dirtyFields, isDirty } = formState

  // Layout, not passive: when the catalog resolves before the settings the form mounts in the
  // same commit the reset belongs to, and a passive effect would let the uncontrolled select
  // paint its first option for a frame before taking the persisted value.
  useLayoutEffect(() => {
    const s = settings.data
    // A refetch bringing someone else's save must not wipe an edit in progress — the unsaved
    // guard below promises the operator exactly that.
    if (!s || isDirty) return
    reset({
      default_license_id: s.default_license_id,
      invite_only: s.invite_only,
      email_verification_ttl_hours: s.email_verification_ttl_hours,
      password_min_length: s.password_min_length,
      password_require_uppercase: s.password_require_uppercase,
      password_require_digit: s.password_require_digit,
      password_require_symbol: s.password_require_symbol,
      password_reset_cooldown_seconds: s.password_reset_cooldown_seconds,
      password_reset_max_per_day: s.password_reset_max_per_day,
    })
  }, [settings.data, isDirty, reset])

  useUnsavedGuard(isDirty)

  const onSubmit = handleSubmit(async (values) => {
    // Only the touched knobs: the singleton is shared, so a full snapshot would silently revert
    // a field another admin changed since this page loaded.
    const body: PlatformSettingsUpdate = {}
    if (dirtyFields.default_license_id) body.default_license_id = values.default_license_id
    if (dirtyFields.invite_only) body.invite_only = values.invite_only
    if (dirtyFields.email_verification_ttl_hours)
      body.email_verification_ttl_hours = values.email_verification_ttl_hours
    if (dirtyFields.password_min_length) body.password_min_length = values.password_min_length
    if (dirtyFields.password_require_uppercase)
      body.password_require_uppercase = values.password_require_uppercase
    if (dirtyFields.password_require_digit)
      body.password_require_digit = values.password_require_digit
    if (dirtyFields.password_require_symbol)
      body.password_require_symbol = values.password_require_symbol
    if (dirtyFields.password_reset_cooldown_seconds)
      body.password_reset_cooldown_seconds = values.password_reset_cooldown_seconds
    if (dirtyFields.password_reset_max_per_day)
      body.password_reset_max_per_day = values.password_reset_max_per_day

    try {
      await update.mutateAsync(body)
      // Re-baseline so the unsaved guard stands down after a save.
      reset(values)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader
        title="System Preferences"
        description="Platform-wide settings — apply to every user."
      />

      {failed ? (
        <p className="text-destructive">{humanizeError(settings.error ?? licenses.error)}</p>
      ) : (
        (settings.isPending || licenses.isPending) && <DetailSkeleton />
      )}

      {settings.data && licenses.isSuccess && (
        <form onSubmit={onSubmit} className="space-y-6" noValidate>
          {!canUpdate && (
            <p className="text-muted-foreground text-sm">
              You have read-only access to these settings.
            </p>
          )}

          <fieldset disabled={!canUpdate} className="space-y-6">
            <SettingsSection
              title="Data licensing"
              description="The default license evaluation data is shared under. Evaluation groups and evaluations may override it."
            >
              <LicensePicker
                field={register('default_license_id')}
                value={defaultLicenseId}
                error={formState.errors.default_license_id?.message}
                // No `offerNoLicense`: the PATCH rejects that entry, since a platform-wide
                // "no license" would unlicense everything that inherits.
                id="default_license_id"
              />
            </SettingsSection>

            <SettingsSection
              title="Access & registration"
              description="Who can create an account on this platform."
            >
              <Field orientation="horizontal">
                {/* `aria-describedby` is explicit: `Field` does not wire it. */}
                <Checkbox
                  id="invite_only"
                  aria-describedby="invite-only-hint"
                  checked={inviteOnly}
                  onCheckedChange={(next) =>
                    setValue('invite_only', next === true, { shouldDirty: true })
                  }
                />
                <FieldContent>
                  <FieldLabel htmlFor="invite_only">Invite-only mode</FieldLabel>
                  <FieldDescription id="invite-only-hint">
                    Hides the sign-up form and refuses open registration. New users join via
                    invitations only; in-flight email verifications still complete.
                  </FieldDescription>
                </FieldContent>
              </Field>
              <div className="mt-4 space-y-1">
                <FormField
                  label="Verification link expiry (hours)"
                  htmlFor="email_verification_ttl_hours"
                  error={formState.errors.email_verification_ttl_hours?.message}
                >
                  <Input
                    id="email_verification_ttl_hours"
                    type="number"
                    min={1}
                    max={8760}
                    className="max-w-40"
                    aria-describedby="ttl-hint"
                    {...register('email_verification_ttl_hours', { valueAsNumber: true })}
                  />
                </FormField>
                <p id="ttl-hint" className="text-muted-foreground text-xs">
                  How long a sign-up confirmation link stays valid (1–8760 hours). Applies to links
                  issued from now on.
                </p>
              </div>
            </SettingsSection>

            <SettingsSection
              title="Passwords"
              description="What a new password must satisfy, and how often a reset link can be requested."
            >
              <div className="space-y-1">
                <FormField
                  label="Minimum length (characters)"
                  htmlFor="password_min_length"
                  error={formState.errors.password_min_length?.message}
                >
                  <Input
                    id="password_min_length"
                    type="number"
                    min={8}
                    max={128}
                    className="max-w-40"
                    aria-describedby="min-length-hint"
                    {...register('password_min_length', { valueAsNumber: true })}
                  />
                </FormField>
                <p id="min-length-hint" className="text-muted-foreground text-xs">
                  8–128 characters. Applies when a password is set — existing passwords keep
                  working.
                </p>
              </div>

              <FieldSet className="mt-4">
                <FieldLegend variant="label">Required characters</FieldLegend>
                <FieldGroup data-slot="checkbox-group">
                  {(
                    [
                      ['password_require_uppercase', 'An uppercase letter'],
                      ['password_require_digit', 'A digit'],
                      [
                        'password_require_symbol',
                        'A symbol (anything that is not a letter or a number)',
                      ],
                    ] as const
                  ).map(([name, label]) => (
                    <Field key={name} orientation="horizontal">
                      {/* The hint hangs off each checkbox, not the fieldset: a description on the
                          group is not reliably announced once focus lands on a child input. */}
                      <Checkbox
                        id={name}
                        aria-describedby="required-characters-hint"
                        checked={requiredChars[name]}
                        onCheckedChange={(next) =>
                          setValue(name, next === true, { shouldDirty: true })
                        }
                      />
                      <FieldLabel htmlFor={name} className="font-normal">
                        {label}
                      </FieldLabel>
                    </Field>
                  ))}
                  <FieldDescription id="required-characters-hint">
                    Off by default. Common and breached passwords are always rejected, as are ones
                    resembling the account&apos;s email or name.
                  </FieldDescription>
                </FieldGroup>
              </FieldSet>

              <div className="mt-4 space-y-1">
                <FormField
                  label="Reset link cooldown (seconds)"
                  htmlFor="password_reset_cooldown_seconds"
                  error={formState.errors.password_reset_cooldown_seconds?.message}
                >
                  <Input
                    id="password_reset_cooldown_seconds"
                    type="number"
                    min={0}
                    max={3600}
                    className="max-w-40"
                    aria-describedby="cooldown-hint"
                    {...register('password_reset_cooldown_seconds', { valueAsNumber: true })}
                  />
                </FormField>
                <p id="cooldown-hint" className="text-muted-foreground text-xs">
                  Minimum gap between two self-service reset requests for one account (0–3600; 0
                  turns the cooldown off). An admin-triggered reset ignores it, but its send still
                  starts the gap.
                </p>
              </div>

              <div className="mt-4 space-y-1">
                <FormField
                  label="Reset links per day"
                  htmlFor="password_reset_max_per_day"
                  error={formState.errors.password_reset_max_per_day?.message}
                >
                  <Input
                    id="password_reset_max_per_day"
                    type="number"
                    min={1}
                    max={100}
                    className="max-w-40"
                    aria-describedby="daily-cap-hint"
                    {...register('password_reset_max_per_day', { valueAsNumber: true })}
                  />
                </FormField>
                <p id="daily-cap-hint" className="text-muted-foreground text-xs">
                  How many self-service reset requests one account can make per rolling 24 hours
                  (1–100). Requests past the cap are accepted silently and send nothing. An
                  admin-triggered reset ignores the cap but counts towards it.
                </p>
              </div>
            </SettingsSection>
          </fieldset>

          <div
            className={cn('flex items-center gap-3', canUpdate ? 'justify-between' : 'justify-end')}
          >
            {canUpdate && (
              <div className="flex items-center gap-3">
                <Button type="submit" disabled={formState.isSubmitting || !isDirty}>
                  {formState.isSubmitting ? 'Saving…' : 'Save changes'}
                </Button>
                {isDirty && !formState.isSubmitting && (
                  <span className="text-muted-foreground text-sm">Unsaved changes</span>
                )}
              </div>
            )}
            <p className="text-muted-foreground text-xs">
              {settings.data.updated_at
                ? `Last changed ${new Date(settings.data.updated_at).toLocaleString()}`
                : 'Using shipped defaults — never overridden.'}
            </p>
          </div>
        </form>
      )}

      {/* Its own card outside the settings form: terms are a separate resource with their own
          POST, not a knob on the singleton. */}
      <TermsSettingsSection />
    </div>
  )
}
