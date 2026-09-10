import { Badge } from '@/components/ui/badge'
import { annotationLabel } from './annotation-index'
import type { AnnotationResponse } from '@/lib/api/types'

/**
 * A message's labels, rendered inline as chips.
 *
 * Read-only: adding and removing happens in the dialog, so a chip here never carries a
 * remove affordance — otherwise every transcript row would offer a destructive control
 * for a colleague's label that the server refuses with a 403.
 *
 * A catalog label and an ad-hoc one look identical on purpose: the distinction matters to
 * aggregation, not to the person reading the transcript.
 */
export function AnnotationChips({ annotations }: { annotations: AnnotationResponse[] }) {
  if (annotations.length === 0) return null
  return (
    <ul role="list" className="flex flex-wrap gap-1 pt-0.5" data-testid="message-annotations">
      {annotations.map((annotation) => (
        <li key={annotation.id}>
          <Badge
            variant="tag"
            // `current`, not `primary`: these sit on `bg-primary` (user) or `bg-card`
            // (assistant), and a hardcoded primary tint composites to nothing on the first —
            // the same rule the sibling tag row in `bubble.tsx` follows.
            className="max-w-[14rem] rounded-full border-current/20 bg-current/10 px-2 text-[10px] text-current"
            title={
              annotation.label.is_custom
                ? `${annotationLabel(annotation)} — a label its author typed`
                : annotationLabel(annotation)
            }
          >
            <span className="truncate">{annotationLabel(annotation)}</span>
          </Badge>
        </li>
      ))}
    </ul>
  )
}
