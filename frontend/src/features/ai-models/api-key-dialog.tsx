import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useSetApiKey } from './mutations'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

const schema = z.object({ value: z.string().min(1, 'Required') })
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { value: '' }

export function ApiKeyDialog({
  modelId,
  open,
  onOpenChange,
}: {
  modelId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const setKey = useSetApiKey(modelId)
  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onSubmit = handleSubmit(async ({ value }) => {
    try {
      await setKey.mutateAsync(value)
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Set API key" onOpen={() => reset(EMPTY)}>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="API key" htmlFor="api-key-input" error={formState.errors.value?.message}>
          <Input id="api-key-input" type="password" {...register('value')} />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Saving…' : 'Set'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
