import { useState } from 'react'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { ApiError, humanizeError } from '@/lib/api/problem'
import type { ConversationGroupResponse, EvaluationLabelCategoryResponse } from '@/lib/api/types'
import { heldLabels, labelPayload, type LabelValues } from './label-values'
import { LabelsFields } from './labels-fields'
import { useSetGroupLabels } from './mutations'

// The server matches a listed tag ignoring case, so a held `polish` under a list now spelling it
// `Polish` is that option; seeded as stored, its checkbox would show unticked and could not clear it.
const seed = (held: LabelValues, categories: EvaluationLabelCategoryResponse[]): LabelValues =>
  Object.fromEntries(
    Object.entries(held).map(([id, picked]) => {
      const listed = categories.find((c) => c.id === id)?.values ?? []
      return [id, picked.map((v) => listed.find((x) => x.toLowerCase() === v.toLowerCase()) ?? v)]
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
      title={group.labels?.length ? 'Edit tags' : 'Add tags'}
      onOpen={() => {
        save.reset()
        setValue(seed(heldLabels(group.labels), categories))
      }}
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          // Every category on offer plus every one the group holds: the set is replaced, so a tag
          // under a category missing from a stale list must be sent back, while one whose category
          // was deleted since the dialog opened must not be.
          const ids = new Set([
            ...categories.map((c) => c.id),
            ...Object.keys(heldLabels(group.labels)),
          ])
          const labels = labelPayload(
            Object.fromEntries(Object.entries(value).filter(([id]) => ids.has(id))),
          )
          save.mutate({ groupId: group.id, labels }, { onSuccess: () => onOpenChange(false) })
        }}
      >
        <p className="text-muted-foreground text-sm">
          Custom tags describe this conversation for exports and reports. They are never sent to the
          model.
        </p>
        <LabelsFields
          categories={categories}
          value={value}
          onChange={setValue}
          held={heldLabels(group.labels)}
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
