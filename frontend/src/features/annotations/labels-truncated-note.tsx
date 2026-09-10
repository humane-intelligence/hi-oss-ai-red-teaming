import { cn } from '@/lib/utils'
import { ANNOTATIONS_PAGE_SIZE } from './queries'

/**
 * Says so when a conversation carries more annotations than one page holds.
 *
 * The read is a single page of annotation *rows* ordered newest-first, and one message can
 * carry several (a row per label per author). So past the cap an older message shows no chips,
 * or only the newest of its labels — either way indistinguishable from the whole truth, which
 * is the misread the inline error surface beside this one exists to prevent.
 *
 * Renders nothing below the cap, so a host may style it as its own strip (`className`) without
 * leaving an empty one behind.
 */
export function LabelsTruncatedNote({
  total,
  className,
}: {
  total: number | undefined
  className?: string
}) {
  if ((total ?? 0) <= ANNOTATIONS_PAGE_SIZE) return null
  return (
    <p className={cn('text-muted-foreground text-xs', className)} data-testid="labels-truncated">
      Showing the {ANNOTATIONS_PAGE_SIZE} most recent labels of {total} on this conversation;
      messages labelled earlier may show none, or only some.
    </p>
  )
}
