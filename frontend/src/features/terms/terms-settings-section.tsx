import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { FormField } from '@/components/shared/form-field'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useCurrentTerms, useTermsVersions } from './queries'
import { usePublishTerms } from './mutations'
import { TermsDialogLink } from './terms-dialog'

const schema = z.object({
  version: z.string().trim().min(1, 'Required').max(64, 'At most 64 characters'),
  content: z.string().trim().min(1, 'Required'),
})
type FormValues = z.infer<typeof schema>
const PUBLISH_FIELDS = ['version', 'content'] as const

export function TermsSettingsSection() {
  const { has } = usePermissions()
  const canPublish = has('platform_settings:update')
  const current = useCurrentTerms()
  const versions = useTermsVersions()
  const publish = usePublishTerms()
  const [pending, setPending] = useState<FormValues | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  // By identity rather than `slice(1)`: position only means "superseded" while the list and the
  // current read agree on ordering, and nothing here would notice if they stopped.
  const supersededVersions = (versions.data?.items ?? []).filter((v) => v.id !== current.data?.id)
  const { register, handleSubmit, setError, reset, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { version: '', content: '' },
  })
  useUnsavedGuard(formState.isDirty)

  const doPublish = async () => {
    if (!pending) return
    setFormError(null)
    try {
      // The values zod parsed (trimmed), not a re-read of the raw inputs.
      await publish.mutateAsync(pending)
      setPending(null)
      reset({ version: '', content: '' })
    } catch (err) {
      setPending(null)
      // A duplicate version comes back as a 409 carrying `errors[]` on the field.
      if (!applyApiError(err, setError, PUBLISH_FIELDS)) setFormError(humanizeError(err))
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Terms of service</CardTitle>
        <p className="text-muted-foreground text-sm">
          The version every account must accept. Versions are immutable — correcting text means
          publishing a new one.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="text-sm">
          {current.isPending && <p className="text-muted-foreground">Loading…</p>}
          {current.isError && (
            <p role="alert" className="text-destructive">
              {humanizeError(current.error)}
            </p>
          )}
          {current.data ? (
            <>
              <p>
                Current version <span className="font-medium">{current.data.version}</span>,
                published {new Date(current.data.published_at).toLocaleString()}.
              </p>
              <div className="mt-1 text-xs">
                <TermsDialogLink terms={current.data} />
              </div>
            </>
          ) : (
            current.isSuccess && (
              <p className="text-muted-foreground">
                Nothing published yet, so no account is asked to accept anything.
              </p>
            )
          )}
        </div>

        {versions.isError && (
          <p role="alert" className="text-destructive text-sm">
            {humanizeError(versions.error)}
          </p>
        )}

        {supersededVersions.length > 0 && (
          <div className="text-sm">
            <p className="font-medium">History</p>
            <ul className="text-muted-foreground mt-1 space-y-0.5 text-xs">
              {supersededVersions.map((version) => (
                <li key={version.id}>
                  {version.version} — {new Date(version.published_at).toLocaleString()}
                </li>
              ))}
            </ul>
          </div>
        )}

        {canPublish && (
          <form
            onSubmit={handleSubmit((values) => setPending(values))}
            className="space-y-4 border-t pt-6"
            noValidate
          >
            <p className="text-sm font-medium">Publish a new version</p>
            <FormField label="Version" htmlFor="version" error={formState.errors.version?.message}>
              <Input id="version" placeholder="1.0" {...register('version')} />
            </FormField>
            <FormField
              label="Text (Markdown)"
              htmlFor="content"
              error={formState.errors.content?.message}
            >
              <Textarea id="content" rows={10} {...register('content')} />
            </FormField>
            {formError && (
              <p role="alert" className="text-destructive text-sm">
                {formError}
              </p>
            )}
            <div className="flex justify-end">
              <Button type="submit" disabled={publish.isPending}>
                {publish.isPending ? 'Publishing…' : 'Publish'}
              </Button>
            </div>
          </form>
        )}

        <ConfirmDialog
          open={pending !== null}
          onOpenChange={(open) => !open && setPending(null)}
          title="Publish these terms?"
          // Publishing is not a quiet settings write: the API refuses every account that has not
          // accepted, the publishing admin included, and the text cannot be edited afterwards.
          description="Every account — including yours — is locked out until it accepts this version: the API refuses it, not just the console. The text cannot be edited once published, so correcting it means publishing another version, which you can only do after accepting this one."
          confirmLabel="Publish"
          pending={publish.isPending}
          onConfirm={doPublish}
        />
      </CardContent>
    </Card>
  )
}
