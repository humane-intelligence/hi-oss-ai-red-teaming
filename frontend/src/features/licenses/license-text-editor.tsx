import { useState } from 'react'
import { useUpdateLicense } from './mutations'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Modal } from '@/components/ui/modal'
import { Textarea } from '@/components/ui/textarea'

// The one field of a curated licence the API accepts (`licenses:manage` only). Opened only for a row
// the catalog ships no text for, so the resync leaves this column
// alone and what is typed here survives a deploy.
export function LicenseTextEditor({
  licenseId,
  licenseName,
  current,
  open,
  onOpenChange,
}: {
  licenseId: string
  licenseName: string
  current: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [draft, setDraft] = useState(current)
  const update = useUpdateLicense(licenseId)

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={licenseName}
      eyebrow="License text"
      className="max-w-2xl"
      busy={update.isPending}
      onOpen={() => setDraft(current)}
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="license_content">License text</Label>
          <Textarea
            id="license_content"
            rows={14}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
        </div>
        <p className="text-muted-foreground text-xs">
          The catalog ships no text for this license, so a resync leaves what you save here alone.
          Every other field of a curated license stays managed in code.
        </p>
        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={update.isPending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={update.isPending || draft.trim().length === 0}
            onClick={() =>
              update.mutate({ content: draft }, { onSuccess: () => onOpenChange(false) })
            }
          >
            {update.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
