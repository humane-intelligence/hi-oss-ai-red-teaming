import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/modal'
import { TermsText } from './terms-text'
import type { TermsDocumentResponse } from '@/lib/api/types'

// The "read it before you tick it" affordance, shared by the two signup forms and the account
// card. A dialog rather than a route: the reader is mid-form, and navigating away would cost them
// what they have typed.
export function TermsDialogLink({ terms }: { terms: TermsDocumentResponse }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="underline underline-offset-2">
        Read the terms of service
      </button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="Terms of service"
        eyebrow={`Version ${terms.version}`}
        className="max-w-2xl"
        dismissable
      >
        <div className="space-y-4">
          <TermsText content={terms.content} label={`Terms of service, version ${terms.version}`} />
          <div className="flex justify-end">
            <Button type="button" variant="outline" size="sm" onClick={() => setOpen(false)}>
              Close
            </Button>
          </div>
        </div>
      </Modal>
    </>
  )
}
