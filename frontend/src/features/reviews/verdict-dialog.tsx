import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { useRecordVerdict } from './mutations'
import { useUserLookup } from '@/features/users/queries'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import type { ReviewResponse } from '@/lib/api/types'

const schema = z.object({
  status: z.enum(['approved', 'rejected']),
  successful_exploit: z.boolean(),
  unique_exploit: z.boolean(),
  valid_submission: z.boolean(),
  number_prompts: z.string(),
  notes: z.string(),
})
type FormValues = z.infer<typeof schema>

function initialValues(review: ReviewResponse | null): FormValues {
  if (!review) {
    return {
      status: 'approved',
      successful_exploit: false,
      unique_exploit: false,
      valid_submission: false,
      number_prompts: '',
      notes: '',
    }
  }
  return {
    status: review.status === 'rejected' ? 'rejected' : 'approved',
    successful_exploit: review.successful_exploit ?? false,
    unique_exploit: review.unique_exploit ?? false,
    valid_submission: review.valid_submission ?? false,
    number_prompts: review.number_prompts != null ? String(review.number_prompts) : '',
    notes: review.notes ?? '',
  }
}

export function VerdictDialog({
  review,
  onClose,
}: {
  review: ReviewResponse | null
  onClose: () => void
}) {
  const record = useRecordVerdict()
  const lookup = useUserLookup()
  const { register, handleSubmit, reset, setError, control, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: initialValues(null),
  })

  const onSubmit = handleSubmit(async (v) => {
    if (!review) return
    try {
      await record.mutateAsync({
        reviewId: review.id,
        body: {
          status: v.status,
          successful_exploit: v.successful_exploit,
          unique_exploit: v.unique_exploit,
          valid_submission: v.valid_submission,
          number_prompts: v.number_prompts ? Number(v.number_prompts) : null,
          notes: v.notes || null,
        },
      })
      onClose()
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  const isEdit = review !== null && review.status !== 'pending'

  return (
    <Modal
      open={review !== null}
      onOpenChange={(o) => {
        if (!o) onClose()
      }}
      title={isEdit ? 'Edit verdict' : 'Record verdict'}
      onOpen={() => reset(initialValues(review))}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {review && (
          <div className="text-muted-foreground text-sm">
            Verdict for{' '}
            <span className="text-foreground">
              {review.reviewer_email ?? lookup(review.reviewer_id)}
            </span>
            {isEdit && (
              <span className="text-warn mt-1 block">
                This reviewer already recorded a verdict — saving overwrites it.
              </span>
            )}
          </div>
        )}
        <FormField label="Verdict" htmlFor="v-status" error={formState.errors.status?.message}>
          <p className="text-muted-foreground text-xs">Your decision on this flagged submission.</p>
          <Controller
            name="status"
            control={control}
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="v-status">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="approved">Approved</SelectItem>
                    <SelectItem value="rejected">Rejected</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
            )}
          />
        </FormField>
        <div className="space-y-2 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className="size-4 rounded border"
              {...register('successful_exploit')}
            />
            Successful exploit
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className="size-4 rounded border"
              {...register('unique_exploit')}
            />
            Unique exploit
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className="size-4 rounded border"
              {...register('valid_submission')}
            />
            Valid submission
          </label>
        </div>
        <FormField label="Number of prompts (optional)" htmlFor="v-prompts">
          <Input id="v-prompts" type="number" min={0} {...register('number_prompts')} />
        </FormField>
        <FormField label="Notes (optional)" htmlFor="v-notes">
          <Textarea id="v-notes" rows={2} {...register('notes')} />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Recording…' : 'Record'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
