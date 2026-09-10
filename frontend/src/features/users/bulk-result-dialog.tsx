import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'

// Structural rather than the generated `BulkResponse_*_` types: the envelopes differ only in the
// per-row `data`, which this view never reads.
export type BulkOutcome = {
  total: number
  succeeded: number
  failed: number
  results: {
    row_key: string
    status: 'ok' | 'failed'
    error?: { title: string; detail?: string | null } | null
  }[]
}

export function BulkResultSummary({
  result,
  // `data` is null on failed rows, so only the caller can name them — from the request side.
  labelFor,
}: {
  result: BulkOutcome
  labelFor: (rowKey: string) => string
}) {
  const failed = result.results.filter((r) => r.status === 'failed')
  return (
    <div className="space-y-2">
      <p className="text-sm">
        {result.succeeded} of {result.total} succeeded
        {result.failed > 0 && `, ${result.failed} failed`}.
      </p>
      {failed.length > 0 && (
        <ul className="list-disc space-y-1 pl-4">
          {failed.map((r) => (
            <li key={r.row_key} className="text-sm">
              <span className="font-medium">{labelFor(r.row_key)}</span>:{' '}
              {r.error?.detail ?? r.error?.title ?? 'Unknown error'}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function BulkResultDialog({
  open,
  onOpenChange,
  title,
  result,
  labelFor,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  result: BulkOutcome | undefined
  labelFor: (rowKey: string) => string
}) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={title} className="max-w-lg">
      {result && <BulkResultSummary result={result} labelFor={labelFor} />}
      <div className="flex justify-end">
        <Button onClick={() => onOpenChange(false)}>Close</Button>
      </div>
    </Modal>
  )
}
