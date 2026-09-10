import { useLicense } from './queries'
import { isHttpUrl } from './url'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/modal'
import { humanizeError } from '@/lib/api/problem'
import type { DataLicenseSummary } from '@/lib/api/types'

// A licence's own text, for a reader who cannot reach `/licenses/{id}` — those pages are gated on
// `licenses:*`, while the text itself is auth-only on the API. Opened from `LicenseValue`.
export function LicenseTextDialog({
  license,
  open,
  onOpenChange,
}: {
  license: DataLicenseSummary
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={license.name}
      eyebrow="Data license"
      className="max-w-2xl"
      dismissable
    >
      <div className="space-y-4">
        {license.short_description && (
          <p className="text-muted-foreground text-sm">{license.short_description}</p>
        )}
        {/* `Modal` renders its children only while open, so the text is fetched on first open — and a
            licence that renders as a plain label never needs a query client at all. */}
        <LicenseText licenseId={license.id} licenseName={license.name} />
        <div className="flex items-center justify-between gap-4">
          {isHttpUrl(license.reference_url) ? (
            <a
              href={license.reference_url}
              target="_blank"
              rel="noreferrer"
              className="text-muted-foreground hover:text-foreground text-xs underline-offset-2 hover:underline"
            >
              Canonical text ↗
            </a>
          ) : (
            <span />
          )}
          <Button type="button" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </div>
      </div>
    </Modal>
  )
}

function LicenseText({ licenseId, licenseName }: { licenseId: string; licenseName: string }) {
  // Renders its own failure, so it opts out of the global error toast — one channel per failure.
  const detail = useLicense(licenseId, { suppressErrorToast: true })

  if (detail.isPending)
    return <p className="text-muted-foreground text-sm">Loading the license text…</p>
  if (detail.isError)
    return <p className="text-destructive text-sm">{humanizeError(detail.error)}</p>
  return (
    // A named, focusable region: a scroll container is no tab stop on its own, and the dialog's other
    // stops don't scroll it — so without this a keyboard-only reader gets the first screenful of a
    // text that runs to tens of KB.
    <pre
      tabIndex={0}
      role="region"
      aria-label={`License text: ${licenseName}`}
      className="bg-muted max-h-[24rem] overflow-auto rounded-md p-4 text-sm whitespace-pre-wrap"
    >
      {detail.data?.content}
    </pre>
  )
}
