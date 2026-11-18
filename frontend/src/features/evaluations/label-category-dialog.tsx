import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { ApiError } from '@/lib/api/problem'
import {
  MAX_LABEL_CATEGORY_NAME_LEN,
  MAX_LABEL_VALUES,
  MAX_LABEL_VALUE_LEN,
} from '@/lib/api/limits'
import type { EvaluationLabelCategoryResponse } from '@/lib/api/types'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useCreateLabelCategory, useUpdateLabelCategory } from './mutations'

// The server drops control and format characters (zero-width spaces and the like) before it checks
// anything, so a line of only those is blank there; dropping them here roughly mirrors that.
const cleanLine = (text: string) => text.replace(/[\p{Cc}\p{Cf}]/gu, '').trim()

const splitValues = (text: string) => text.split('\n').map(cleanLine).filter(Boolean)

const schema = z.object({
  name: z
    .string()
    .transform(cleanLine)
    .pipe(z.string().min(1, 'Required').max(MAX_LABEL_CATEGORY_NAME_LEN)),
  values: z.string().superRefine((text, ctx) => {
    const values = splitValues(text)
    const tooLong = values.find((v) => v.length > MAX_LABEL_VALUE_LEN)
    if (tooLong) {
      ctx.addIssue({
        code: 'custom',
        message: `"${tooLong.slice(0, 20)}…" is longer than ${MAX_LABEL_VALUE_LEN} characters`,
      })
    } else if (values.length > MAX_LABEL_VALUES) {
      ctx.addIssue({ code: 'custom', message: `At most ${MAX_LABEL_VALUES} tags` })
    } else {
      const seen = new Set<string>()
      const dup = values.find((v) => {
        const key = v.toLowerCase()
        if (seen.has(key)) return true
        seen.add(key)
        return false
      })
      if (dup) ctx.addIssue({ code: 'custom', message: `"${dup}" is listed twice` })
    }
  }),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { name: '', values: '' }

type Props = {
  evaluationId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  category?: EvaluationLabelCategoryResponse | null
}

export function LabelCategoryDialog({ evaluationId, open, onOpenChange, category }: Props) {
  const create = useCreateLabelCategory(evaluationId)
  const update = useUpdateLabelCategory(evaluationId)

  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onSubmit = handleSubmit(async ({ name, values }) => {
    try {
      if (category) {
        // Only what differs from what the dialog opened with: a rename must not write back a stale value list.
        const opened = formState.defaultValues
        const list = splitValues(values)
        const body = {
          ...(name !== cleanLine(opened?.name ?? '') && { name }),
          ...(list.join('\n') !== splitValues(opened?.values ?? '').join('\n') && { values: list }),
        }
        if (Object.keys(body).length > 0) {
          await update.mutateAsync({ categoryId: category.id, body })
        }
      } else {
        await create.mutateAsync({ name, values: splitValues(values) })
      }
      onOpenChange(false)
    } catch (err) {
      if (applyApiError(err, setError, ['name', 'values'])) return
      // A 422 on one value is located at `values.<index>`, which no field owns, and 422s are never
      // toasted, so without this the save would fail silently.
      if (err instanceof ApiError && err.status === 422) {
        const reasons = (err.problem.errors ?? []).map((e) => {
          const at = e.loc.at(-1)
          return typeof at === 'number' ? `Tag ${at + 1}: ${e.msg}` : e.msg
        })
        setError('root', { message: reasons.join('; ') || 'The server rejected these tags.' })
      }
    }
  })

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      busy={formState.isSubmitting}
      title={category ? 'Edit tag category' : 'Add tag category'}
      onOpen={() =>
        reset(category ? { name: category.name, values: category.values.join('\n') } : EMPTY)
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Tag category" htmlFor="lc-name" error={formState.errors.name?.message}>
          <Input
            id="lc-name"
            placeholder="e.g. language"
            maxLength={MAX_LABEL_CATEGORY_NAME_LEN}
            {...register('name')}
          />
        </FormField>
        <FormField
          label="Tags"
          htmlFor="lc-values"
          description={
            category
              ? 'One tag per line. Leave empty for free text. Conversations keep a tag you remove here.'
              : 'One tag per line. Leave empty to let each conversation take any text.'
          }
          error={formState.errors.values?.message}
        >
          <Textarea
            id="lc-values"
            rows={5}
            placeholder={'e.g. english\npolish'}
            {...register('values')}
          />
        </FormField>
        {formState.errors.root?.message && (
          <p role="alert" className="text-destructive text-sm">
            {formState.errors.root.message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={formState.isSubmitting}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Saving…' : category ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
