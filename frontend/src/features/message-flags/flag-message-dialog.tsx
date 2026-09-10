import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useCreateFlag } from './mutations'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

const schema = z.object({
  reason: z.string().min(1, 'Required'),
  comment: z.string(),
  red_flagged: z.boolean(),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { reason: '', comment: '', red_flagged: true }

type Props = {
  conversationId: string
  messageIds: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onSuccess?: () => void
}

export function FlagMessageDialog({
  conversationId,
  messageIds,
  open,
  onOpenChange,
  onSuccess,
}: Props) {
  const create = useCreateFlag(conversationId)
  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onSubmit = handleSubmit(async (values) => {
    try {
      await create.mutateAsync({
        conversation_id: conversationId,
        message_ids: messageIds,
        reason: values.reason,
        red_flagged: values.red_flagged,
        comment: values.comment || null,
      })
      onSuccess?.()
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={messageIds.length > 1 ? `Flag ${messageIds.length} messages` : 'Flag message'}
      onOpen={() => reset(EMPTY)}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Reason" htmlFor="flag-reason" error={formState.errors.reason?.message}>
          <Textarea id="flag-reason" rows={3} {...register('reason')} />
        </FormField>
        <FormField
          label="Comment (optional)"
          htmlFor="flag-comment"
          error={formState.errors.comment?.message}
        >
          <Textarea id="flag-comment" rows={2} {...register('comment')} />
        </FormField>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 size-4 rounded border"
            {...register('red_flagged')}
          />
          <span>
            Exploit-worthy
            <span className="text-muted-foreground block text-xs">
              The model was actually broken here — flags it as a successful-exploit candidate for
              review.
            </span>
          </span>
        </label>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Flagging…' : 'Flag'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
