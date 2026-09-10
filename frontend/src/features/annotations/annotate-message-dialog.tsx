import { useId, useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { MultiSearchSelect } from '@/components/shared/multi-search-select'
import { usePermissions } from '@/lib/auth/use-permissions'
import { ANNOTATIONS_PAGE_SIZE, useAnnotationLabels } from './queries'
import { useCreateAnnotation, useDeleteAnnotation } from './mutations'
import { annotationLabel, annotationValue } from './annotation-index'
import type { AnnotationResponse } from '@/lib/api/types'

// openapi-typescript drops `maxLength`, so this mirrors by hand the cap the create payload
// puts on a label name, and drifts silently if that cap moves.
export const ANNOTATION_TEXT_MAX_LENGTH = 128

type Props = {
  messageId: string
  /** Scopes the suggestions: labels already used in this conversation are offered too. */
  conversationId: string
  /** Every author's annotations on this message — the caller's own are the editable ones. */
  annotations: AnnotationResponse[]
  /** The viewer, so their own labels can be told from a colleague's. */
  currentUserId?: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Add and remove labels on one message.
 *
 * Each toggle is its own request, applied immediately — the API is one row per
 * (message, label, author), so there is no array to submit and nothing to diff on close.
 * Create is idempotent server-side, which is what makes a double-click harmless.
 *
 * Only the caller's own labels are removable here: another annotator's are shown for
 * context (that is the point of shared reads) but deleting one is refused with a 403.
 */
export function AnnotateMessageDialog({
  messageId,
  conversationId,
  annotations,
  currentUserId,
  open,
  onOpenChange,
}: Props) {
  const labelsQ = useAnnotationLabels(conversationId, open)
  const create = useCreateAnnotation()
  const remove = useDeleteAnnotation()
  // Tie the remove affordance to the key that authorises it. Every role holding
  // `annotations:create` also holds `annotations:delete` today, so this changes nothing —
  // it keeps the two from drifting if a custom role ever splits them.
  const canDelete = usePermissions().has('annotations:delete')
  const [term, setTerm] = useState('')
  const hintId = useId()
  const capId = useId()
  const fieldId = useId()

  const mine = canDelete ? annotations.filter((a) => a.created_by_id === currentUserId) : []
  const others = annotations.filter((a) => !mine.includes(a))
  const value = mine.map(annotationValue)
  const byValue = new Map(mine.map((a) => [annotationValue(a), a]))

  // Drives both the caveat below and its place in the field's accessible description: a
  // screen-reader user needs "the search only filters what is loaded" *before* typing, not after.
  const truncated = (labelsQ.data?.total ?? 0) > ANNOTATIONS_PAGE_SIZE
  const offered = labelsQ.data?.items ?? []
  const knownIds = new Set(offered.map((label) => label.id))
  // Labels already applied here that the offered list does not carry — a **retired** entry is
  // the case that matters: the picker never lists it, but the annotation still embeds it, and
  // without this the chip would fall back to rendering the raw uuid.
  const applied = mine
    .filter((a) => !knownIds.has(a.label.id))
    .map((a) => ({ id: a.label.id, name: a.label.name, is_custom: a.label.is_custom }))
  const options = [...offered, ...applied]
    .filter((label) => label.name.toLowerCase().includes(term.toLowerCase()))
    // `hint` tells the two kinds apart in the list without changing the chip, which names
    // the label either way.
    .map((label) => ({
      value: label.id,
      label: label.name,
      hint: label.is_custom ? 'custom' : undefined,
    }))
  // So an applied-but-unoffered label sends `label_id` while it is on screen; removing it drops
  // it from `applied`, after which retyping its wording goes out as `text`.
  for (const label of applied) knownIds.add(label.id)

  const apply = (next: string[]) => {
    const added = next.filter((v) => !value.includes(v))
    const removed = value.filter((v) => !next.includes(v))
    for (const v of added) {
      if (v.length > ANNOTATION_TEXT_MAX_LENGTH && !knownIds.has(v)) {
        toast.error(`Labels are limited to ${ANNOTATION_TEXT_MAX_LENGTH} characters`)
        continue
      }
      // A known id is a pick; anything else is a name the annotator typed, which the server
      // resolves to the shared label of that wording if there is one, else theirs, else a new one.
      create.mutate(
        knownIds.has(v)
          ? { message_id: messageId, label_id: v }
          : { message_id: messageId, text: v },
      )
    }
    for (const v of removed) {
      const annotation = byValue.get(v)
      if (annotation) remove.mutate(annotation.id)
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      // One dialog serves every message on the page, so `term` outlives the message it was typed
      // on: `Modal` unmounts its children, which resets the picker, but not this component's state.
      // A stale term filters the options invisibly, and any picked label it filters out renders as
      // its raw uuid. The picker's mount debounce clears it ~300ms later, so this closes a flash.
      onOpen={() => setTerm('')}
      title="Label this message"
      // Every change is applied on toggle, so there is no unsaved input a stray click could
      // lose — but a write in flight still holds the dialog, since its failure toast is the
      // only report and Esc would otherwise close over it.
      dismissable
      busy={create.isPending || remove.isPending}
    >
      <div className="space-y-4">
        <MultiSearchSelect
          label="Labels"
          htmlFor={fieldId}
          value={value}
          onChange={apply}
          onSearchChange={setTerm}
          options={options}
          isPending={labelsQ.isPending}
          isError={labelsQ.isError}
          error={labelsQ.isError ? 'Could not load the label vocabulary.' : undefined}
          emptyLabel="No matching label."
          placeholder="Search labels, or type your own…"
          allowCreate
          describedBy={truncated ? `${hintId} ${capId}` : hintId}
        />
        <p id={hintId} className="text-muted-foreground text-xs">
          Pick a label, or type your own. Typing the name of a shared label uses that one; anything
          else is saved as yours, offered again on other messages and visible to other annotators
          working this conversation. Changes apply immediately.
        </p>
        {truncated && (
          <p id={capId} className="text-muted-foreground text-xs">
            Showing {ANNOTATIONS_PAGE_SIZE} of {labelsQ.data?.total} labels, and the search filters
            only those. Typing a shared label’s name in full still reuses that one.
          </p>
        )}
        {others.length > 0 && (
          <div className="space-y-1">
            <p className="text-muted-foreground text-xs">
              Also labelled by other annotators (you cannot remove these):
            </p>
            <ul role="list" className="flex flex-wrap gap-1">
              {others.map((annotation) => (
                <li
                  key={annotation.id}
                  className="bg-muted text-muted-foreground rounded-full px-2 py-0.5 text-xs"
                >
                  {annotationLabel(annotation)}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex justify-end">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </div>
      </div>
    </Modal>
  )
}
