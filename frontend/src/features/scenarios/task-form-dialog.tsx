import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useCreateTask, useUpdateTask } from './mutations'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import type { TaskResponse } from '@/lib/api/types'

const schema = z.object({
  name: z.string().min(1, 'Required').max(255),
  description: z.string().min(1, 'Required'),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { name: '', description: '' }

type Props = {
  scenarioId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  task?: TaskResponse | null
}

export function TaskFormDialog({ scenarioId, open, onOpenChange, task }: Props) {
  const isEdit = Boolean(task)
  const create = useCreateTask(scenarioId)
  const update = useUpdateTask(scenarioId)

  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (task) {
        await update.mutateAsync({ taskId: task.id, body: values })
      } else {
        await create.mutateAsync(values)
      }
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? 'Edit task' : 'Add task'}
      onOpen={() => reset(task ? { name: task.name, description: task.description ?? '' } : EMPTY)}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Name" htmlFor="t-name" error={formState.errors.name?.message}>
          <Input id="t-name" {...register('name')} />
        </FormField>
        <FormField
          label="Description"
          htmlFor="t-desc"
          error={formState.errors.description?.message}
        >
          <Textarea id="t-desc" rows={3} {...register('description')} />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
