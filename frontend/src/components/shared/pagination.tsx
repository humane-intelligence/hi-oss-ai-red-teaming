import { useEffect } from 'react'
import { Button } from '@/components/ui/button'

export function Pagination({
  offset,
  limit,
  total,
  onOffsetChange,
  onOffsetCorrect = onOffsetChange,
}: {
  offset: number
  limit: number
  total: number
  onOffsetChange: (offset: number) => void
  /** Called instead of `onOffsetChange` for the automatic step back; a URL-driven list replaces history here. */
  onOffsetCorrect?: (offset: number) => void
}) {
  // Deleting the last row of the last page leaves the offset past the end; step back to the last page.
  const lastOffset = total === 0 ? 0 : Math.floor((total - 1) / limit) * limit
  const shown = Math.min(offset, lastOffset)
  useEffect(() => {
    if (offset > lastOffset) onOffsetCorrect(lastOffset)
  }, [offset, lastOffset, onOffsetCorrect])

  const from = total === 0 ? 0 : shown + 1
  const to = Math.min(shown + limit, total)

  return (
    <div className="text-muted-foreground flex items-center justify-between text-sm">
      <span>
        {from}–{to} of {total}
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={offset === 0}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={offset + limit >= total}
          onClick={() => onOffsetChange(offset + limit)}
        >
          Next
        </Button>
      </div>
    </div>
  )
}
