import { useState, type ReactNode } from 'react'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

type ConfirmDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  confirmLabel?: string
  destructive?: boolean
  pending?: boolean
  // When set, shows a textarea and passes its value to onConfirm.
  reason?: { label: string; required?: boolean; placeholder?: string }
  onConfirm: (reason: string) => void
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  destructive,
  pending,
  reason,
  onConfirm,
}: ConfirmDialogProps) {
  const [reasonValue, setReasonValue] = useState('')
  const canConfirm = !pending && (!reason?.required || reasonValue.trim().length > 0)

  return (
    // `busy` mirrors `pending`: Cancel is already disabled while a write is in flight, and Esc bypasses
    // a disabled button — so without this a destructive confirm closes itself mid-write.
    <Modal
      busy={pending}
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      onOpen={() => setReasonValue('')}
    >
      {description && <div className="text-muted-foreground text-sm">{description}</div>}
      {reason && (
        <div className="space-y-1.5">
          <Label htmlFor="confirm-reason">{reason.label}</Label>
          <Textarea
            id="confirm-reason"
            value={reasonValue}
            onChange={(e) => setReasonValue(e.target.value)}
            placeholder={reason.placeholder}
            rows={3}
          />
        </div>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
          Cancel
        </Button>
        <Button
          disabled={!canConfirm}
          onClick={() => onConfirm(reasonValue.trim())}
          className={
            destructive
              ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90'
              : undefined
          }
        >
          {pending ? 'Working…' : confirmLabel}
        </Button>
      </div>
    </Modal>
  )
}
