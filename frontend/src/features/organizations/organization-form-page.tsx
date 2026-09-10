import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useOrganization } from './queries'
import { useCreateOrganization, useUpdateOrganization } from './mutations'
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
  description: z.string(),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { name: '', description: '' }

export function OrganizationFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const existing = useOrganization(id ?? '')
  const create = useCreateOrganization()
  const update = useUpdateOrganization(id ?? '')

  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  useEffect(() => {
    const o = existing.data
    if (!o) return
    reset({ name: o.name, description: o.description ?? '' })
  }, [existing.data, reset])

  useUnsavedGuard(formState.isDirty)

  const onSubmit = handleSubmit(async (v) => {
    // Explicit null clears the description server-side; empty string would not.
    const body = { name: v.name.trim(), description: v.description.trim() || null }
    try {
      if (isEdit) {
        await update.mutateAsync(body)
        navigate(`/organizations/${id}`)
      } else {
        const created = await create.mutateAsync(body)
        navigate(`/organizations/${created.id}`)
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
        onClick={() => navigate(isEdit ? `/organizations/${id}` : '/organizations')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title={isEdit ? 'Edit organization' : 'New organization'}
        breadcrumbs={
          <Breadcrumbs
            items={[
              { label: 'Organizations', to: '/organizations' },
              { label: isEdit ? 'Edit' : 'New' },
            ]}
          />
        }
      />
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <FormField label="Name" htmlFor="name" error={formState.errors.name?.message}>
              <Input id="name" {...register('name')} />
            </FormField>
            <FormField
              label="Description (optional)"
              htmlFor="description"
              error={formState.errors.description?.message}
            >
              <Textarea id="description" rows={4} {...register('description')} />
            </FormField>
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate(-1)}>
                Cancel
              </Button>
              <Button type="submit" disabled={formState.isSubmitting}>
                {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Create'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
