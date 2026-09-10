import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useLicense } from './queries'
import { useCreateLicense, useUpdateLicense } from './mutations'
import { applyApiError } from '@/lib/api/form'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent } from '@/components/ui/card'

const schema = z.object({
  name: z.string().min(1, 'Required'),
  version: z.string(),
  short_description: z.string().min(1, 'Required'),
  content: z.string().min(1, 'Required'),
  reference_url: z.string(),
  protects_conversation_data: z.boolean(),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = {
  name: '',
  version: '',
  short_description: '',
  content: '',
  reference_url: '',
  protects_conversation_data: false,
}

export function LicenseFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const existing = useLicense(id ?? '')
  const create = useCreateLicense()
  const update = useUpdateLicense(id ?? '')

  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  useEffect(() => {
    const l = existing.data
    if (!l) return
    reset({
      name: l.name,
      version: l.version ?? '',
      short_description: l.short_description,
      content: l.content,
      reference_url: l.reference_url ?? '',
      protects_conversation_data: l.protects_conversation_data,
    })
  }, [existing.data, reset])

  useUnsavedGuard(formState.isDirty)

  // A curated licence is owned by the catalog: every field here is refused (403) and a resync would
  // revert it anyway, so the form is read-only rather than accepting input it cannot save. Its legal
  // text has its own sanctioned path — the editor on the licence page.
  const isCurated = existing.data?.is_curated === true

  const onSubmit = handleSubmit(async (v) => {
    // Empty optional strings become null (clears the column server-side).
    const body = {
      name: v.name.trim(),
      version: v.version.trim() || null,
      short_description: v.short_description.trim(),
      content: v.content,
      reference_url: v.reference_url.trim() || null,
      protects_conversation_data: v.protects_conversation_data,
    }
    try {
      if (isEdit) {
        await update.mutateAsync(body)
        navigate(`/licenses/${id}`)
      } else {
        const created = await create.mutateAsync(body)
        navigate(`/licenses/${created.id}`)
      }
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => navigate(isEdit ? `/licenses/${id}` : '/licenses')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title={isEdit ? 'Edit license' : 'New license'}
        breadcrumbs={
          <Breadcrumbs
            items={[
              { label: 'Data licenses', to: '/licenses' },
              { label: isEdit ? 'Edit' : 'New' },
            ]}
          />
        }
      />
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            {isCurated && (
              <p className="text-muted-foreground rounded-md border p-3 text-sm">
                This license is managed in code — the catalog owns these fields and a resync would
                revert an edit, so they are read-only here.{' '}
                {existing.data?.text_managed_in_code
                  ? 'Its legal text ships in the catalog too, so that changes in code as well.'
                  : 'Its legal text is the exception — the license page carries the editor for it.'}
              </p>
            )}
            {/* The fieldset is the disabled cascade, which reaches every control inside it. It
                carries its own `space-y-4` because the form's applies to direct children only. */}
            <fieldset disabled={isCurated} className="space-y-4">
              <FormField label="Name" htmlFor="name" error={formState.errors.name?.message}>
                <Input id="name" {...register('name')} />
              </FormField>
              <FormField
                label="Version (optional)"
                htmlFor="version"
                error={formState.errors.version?.message}
              >
                <Input id="version" placeholder="e.g. 4.0" {...register('version')} />
              </FormField>
              <FormField
                label="Short description"
                htmlFor="short_description"
                error={formState.errors.short_description?.message}
              >
                <Textarea id="short_description" rows={2} {...register('short_description')} />
              </FormField>
              <FormField
                label="License text"
                htmlFor="content"
                error={formState.errors.content?.message}
              >
                <Textarea
                  id="content"
                  rows={14}
                  className="font-mono text-sm"
                  {...register('content')}
                />
              </FormField>
              <FormField
                label="Reference URL (optional)"
                htmlFor="reference_url"
                error={formState.errors.reference_url?.message}
              >
                <Input id="reference_url" placeholder="https://…" {...register('reference_url')} />
              </FormField>
              {/* Hint as a sibling folded into `aria-describedby`, not inside the label: text inside
                the label becomes the checkbox's accessible *name*, so a screen reader would announce
                the whole paragraph as the control. Same idiom as the licence picker's note. */}
              <div className="flex items-start gap-2 text-sm">
                <input
                  id="protects_conversation_data"
                  type="checkbox"
                  className="mt-0.5 size-4 rounded border"
                  aria-describedby="protects-conversation-data-hint"
                  {...register('protects_conversation_data')}
                />
                <div>
                  <label htmlFor="protects_conversation_data">Protect conversation data</label>
                  <p
                    id="protects-conversation-data-hint"
                    className="text-muted-foreground block text-xs"
                  >
                    Message text written under this licence is stored encrypted at rest — titles,
                    tags and attachments are not. It covers conversations started from now on; ones
                    already running stay unencrypted, including messages added to them later.
                  </p>
                </div>
              </div>
            </fieldset>
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate(-1)}>
                Cancel
              </Button>
              <Button type="submit" disabled={formState.isSubmitting || isCurated}>
                {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Create'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
