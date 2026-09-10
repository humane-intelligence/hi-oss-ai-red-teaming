import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useCreateSavedView, useUpdateSavedView } from './mutations'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { SavedViewResource, SavedViewResponse, SavedViewState } from '@/lib/api/types'

const schema = z.object({ name: z.string().min(1, 'Required').max(128, 'Too long') })
type FormValues = z.infer<typeof schema>

// Create a named view from the current list state, or rename an existing one
// (`view` present → rename mode; only the name is editable).
export function SaveViewDialog({
  open,
  onOpenChange,
  resource,
  view,
  getState,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  resource: SavedViewResource
  view?: SavedViewResponse | null
  getState: () => SavedViewState
}) {
  const create = useCreateSavedView(resource)
  const update = useUpdateSavedView(resource)
  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: '' },
  })

  const onSubmit = handleSubmit(async (values) => {
    const name = values.name.trim()
    try {
      if (view) await update.mutateAsync({ id: view.id, body: { name } })
      else await create.mutateAsync({ resource, name, state: getState() })
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={view ? 'Rename view' : 'Save view'}
      onOpen={() => reset({ name: view?.name ?? '' })}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Name" htmlFor="view-name" error={formState.errors.name?.message}>
          <Input id="view-name" {...register('name')} autoFocus />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Saving…' : view ? 'Rename' : 'Save view'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
