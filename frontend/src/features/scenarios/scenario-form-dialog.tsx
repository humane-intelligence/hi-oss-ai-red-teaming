import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useCreateScenario, useUpdateScenario } from './mutations'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import type { ScenarioResponse } from '@/lib/api/types'

const schema = z.object({
  name: z.string().min(1, 'Required').max(255),
  description: z.string().min(1, 'Required'),
  required_reviews: z.number().int().min(1),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { name: '', description: '', required_reviews: 1 }

type Props = {
  evaluationId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  scenario?: ScenarioResponse | null
}

export function ScenarioFormDialog({ evaluationId, open, onOpenChange, scenario }: Props) {
  const isEdit = Boolean(scenario)
  const create = useCreateScenario(evaluationId)
  const update = useUpdateScenario(evaluationId)

  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (scenario) {
        await update.mutateAsync({ scenarioId: scenario.id, body: values })
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
      title={isEdit ? 'Edit scenario' : 'Add scenario'}
      onOpen={() =>
        reset(
          scenario
            ? {
                name: scenario.name,
                description: scenario.description,
                required_reviews: scenario.required_reviews,
              }
            : EMPTY,
        )
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Name" htmlFor="s-name" error={formState.errors.name?.message}>
          <Input id="s-name" {...register('name')} />
        </FormField>
        <FormField
          label="Description"
          htmlFor="s-desc"
          error={formState.errors.description?.message}
        >
          <Textarea id="s-desc" rows={3} {...register('description')} />
        </FormField>
        <FormField
          label="Required reviewers"
          htmlFor="s-reviews"
          error={formState.errors.required_reviews?.message}
        >
          <Input
            id="s-reviews"
            type="number"
            min={1}
            {...register('required_reviews', { valueAsNumber: true })}
          />
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
