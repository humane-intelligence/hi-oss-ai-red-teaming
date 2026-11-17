import { useState } from 'react'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { ApiError, humanizeError } from '@/lib/api/problem'
import type { ConversationGroupResponse, EvaluationLabelCategoryResponse } from '@/lib/api/types'
import { labelPayload, type LabelValues } from './label-values'
import { LabelsFields } from './labels-fields'
import { useSetGroupLabels } from './mutations'

const heldLabels = (group: ConversationGroupResponse): LabelValues =>
  Object.fromEntries((group.labels ?? []).map((l) => [l.category_id, l.value]))

// The server matches a listed value ignoring case, so a held `polish` under a list now spelling it
// `Polish` is that option; seeded as stored, the select would show none and could not clear it.
const seed = (held: LabelValues, categories: EvaluationLabelCategoryResponse[]): LabelValues =>
  Object.fromEntries(
    Object.entries(held).map(([id, v]) => {
      const listed = categories
        .find((c) => c.id === id)
        ?.values.find((x) => x.toLowerCase() === v.toLowerCase())
      return [id, listed ?? v]
    }),
  )

export function EditLabelsDialog({
  evaluationId,
  group,
  categories,
  open,
  onOpenChange,
}: {
  evaluationId: string
  group: ConversationGroupResponse
  categories: EvaluationLabelCategoryResponse[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const save = useSetGroupLabels(evaluationId)
  const [value, setValue] = useState<LabelValues>({})
  // 422s are never toasted, so this dialog is the only place left to say why Save did nothing.
  const rejected =
    save.error instanceof ApiError && save.error.status === 422
      ? (save.error.problem.errors ?? []).map((e) => e.msg).join('; ') || humanizeError(save.error)
      : null

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      busy={save.isPending}
      title={group.labels?.length ? 'Edit labels' : 'Add labels'}
      onOpen={() => {
        save.reset()
        setValue(seed(heldLabels(group), categories))
      }}
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          // Every category, so one left blank is cleared rather than kept — plus every one the group
          // holds: the set is replaced, so a category missing from a stale list would lose its label.
          const ids = new Set([...categories.map((c) => c.id), ...Object.keys(heldLabels(group))])
          const labels = labelPayload(
            Object.fromEntries([...ids].map((id) => [id, value[id] ?? ''])),
            { keepBlanks: true },
          )
          save.mutate({ groupId: group.id, labels }, { onSuccess: () => onOpenChange(false) })
        }}
      >
        <p className="text-muted-foreground text-sm">
          Labels describe this conversation for exports and reports. They are never sent to the
          model.
        </p>
        <LabelsFields
          categories={categories}
          value={value}
          onChange={setValue}
          held={heldLabels(group)}
          idPrefix="edit-label"
        />
        {rejected && (
          <p role="alert" className="text-destructive text-sm">
            {rejected}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
