import { useState } from 'react'
import { FileText } from 'lucide-react'
import { LicenseTextDialog } from './license-text-dialog'
import { isHttpUrl } from './url'
import type { DataLicenseSummary } from '@/lib/api/types'

// Render a resolved data license (an evaluation/group's `effective_license`), so it needs no catalog
// lookup. Three shapes, in order: a licence whose text the platform holds and still serves opens that
// text in a dialog — the licence pages are gated on `licenses:*`, so for a red-teamer this is the only
// way to read it; otherwise an http(s) `reference_url` links out to the canonical text; otherwise
// plain. Shared by the evaluation and evaluation-group detail pages.
//
// A soft-deleted licence still opens its text: a group keeps resolving one (lineage doesn't lapse)
// and `GET /licenses/{id}` serves it read-only, which is the only way in for the row kind that has no
// canonical URL to fall back to.
export function LicenseValue({ license }: { license: DataLicenseSummary }) {
  const [open, setOpen] = useState(false)
  const label = license.spdx_id ?? license.name
  // The label may be a terse SPDX id — the tooltip carries the full name plus the one-line gist so
  // hovering explains what the licence covers.
  const tooltip = license.short_description
    ? `${license.name} — ${license.short_description}`
    : license.name

  if (license.has_content) {
    return (
      <>
        <button
          type="button"
          title={tooltip}
          aria-haspopup="dialog"
          className="hover:text-foreground underline-offset-2 hover:underline"
          onClick={() => setOpen(true)}
        >
          {label}
          <FileText className="ml-1 inline size-3.5 align-[-2px]" aria-hidden />
        </button>
        <LicenseTextDialog license={license} open={open} onOpenChange={setOpen} />
      </>
    )
  }

  return isHttpUrl(license.reference_url) ? (
    <a
      href={license.reference_url}
      target="_blank"
      rel="noreferrer"
      className="hover:text-foreground underline-offset-2 hover:underline"
      title={tooltip}
    >
      {label}
    </a>
  ) : (
    <span title={tooltip}>{label}</span>
  )
}
