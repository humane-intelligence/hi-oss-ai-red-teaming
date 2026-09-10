import { useLayoutEffect, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { ApiError, fieldErrorsFromProblem } from '@/lib/api/problem'
import { noteErrorMessage } from './stale-session'
import { useCreateNote, useUpdateNote } from './mutations'
import type { NoteResponse } from '@/lib/api/types'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

// openapi-typescript drops `maxLength`, so this mirrors the backend's note-text cap by hand
// and drifts silently if that cap moves.
export const NOTE_MAX_LENGTH = 10_000

const schema = z.object({
  text: z
    .string()
    .min(1, 'Required')
    .max(NOTE_MAX_LENGTH, `Up to ${NOTE_MAX_LENGTH.toLocaleString()} characters`),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { text: '' }

type Props = {
  conversationId: string
  messageIds: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onSuccess?: () => void
  /** Set to edit an existing note instead of creating one: same form, `PATCH` instead of `POST`. */
  note?: NoteResponse
}

export function WriteNoteDialog({
  conversationId,
  messageIds,
  open,
  onOpenChange,
  onSuccess,
  note,
}: Props) {
  const create = useCreateNote()
  // The id is only read on the edit branch; an empty string never reaches the wire.
  const update = useUpdateNote(note?.id ?? '')
  // Re-armed on every change of `open`: a ref cleared only in an unmount cleanup would be
  // stuck at `false` after StrictMode's mount/unmount/remount cycle. Same shape as
  // `conversations/edit-tags-dialog.tsx`.
  const reachable = useRef(open)
  useLayoutEffect(() => {
    reachable.current = open
    return () => {
      reachable.current = false
    }
  }, [open])
  const { register, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const onValid = async (values: FormValues) => {
    try {
      // `text` only on the edit branch: the selection is immutable and `NoteUpdate`
      // forbids extra fields, so sending `message_ids` here would be a 422.
      if (note) await update.mutateAsync({ text: values.text })
      else
        await create.mutateAsync({
          conversation_id: conversationId,
          message_ids: messageIds,
          text: values.text,
        })
      onSuccess?.()
      onOpenChange(false)
    } catch (err) {
      // This dialog is the only channel for its own failures (both mutations opt out of the
      // global toast), and `text` is the only field with a renderer — so a 422 naming anything
      // else (the selection cap, say) must land in `root` instead of a field nobody displays.
      const named = err instanceof ApiError ? fieldErrorsFromProblem(err.problem).text : undefined
      const message = named ?? noteErrorMessage(err)
      // Cancel and a route change can unmount this form while the write is in flight, and an
      // inline error on a surface nobody is looking at reports the failure to nobody.
      if (!reachable.current) toast.error(message)
      else if (named) setError('text', { message: named })
      else setError('root', { message })
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={
        note
          ? 'Edit note'
          : messageIds.length > 1
            ? `Add a note on ${messageIds.length} messages`
            : 'Add a note'
      }
      // Reseeds on every open, so an edit starts from the note's current text and a
      // create starts empty — no effect needed.
      onOpen={() => reset(note ? { text: note.text } : EMPTY)}
      busy={formState.isSubmitting}
    >
      {/* `handleSubmit` is bound at submit time, not during render: the validated callback reads
          the `reachable` ref, and a callback built during render counts as render-phase access. */}
      <form
        onSubmit={(e) => {
          void handleSubmit(onValid)(e)
        }}
        className="space-y-4"
        noValidate
      >
        <p className="text-muted-foreground text-sm">
          Only you and platform admins can see your notes.
        </p>
        <FormField label="Note" htmlFor="note-text" error={formState.errors.text?.message}>
          <Textarea id="note-text" rows={5} {...register('text')} />
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
            disabled={formState.isSubmitting}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          {/* The empty-selection guard is a create concern: an edit carries no selection. */}
          <Button
            type="submit"
            disabled={formState.isSubmitting || (!note && messageIds.length === 0)}
          >
            {formState.isSubmitting ? 'Saving…' : note ? 'Save changes' : 'Save note'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
