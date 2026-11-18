import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { ConversationGroupResponse } from '@/lib/api/types'

// A category picked on its own has no tag to show after it.
const chip = (label: { name: string; value?: string | null }) =>
  label.value ? `${label.name}: ${label.value}` : label.name

export function GroupLabels({
  labels,
  className,
}: {
  labels: ConversationGroupResponse['labels']
  className?: string
}) {
  if (!labels?.length) return null
  return (
    <ul aria-label="Tags" className={cn('flex flex-wrap gap-1.5', className)}>
      {labels.map((label) => (
        <li key={`${label.category_id}:${label.value ?? ''}`}>
          {/* Capped and titled because a tag runs to 200 chars. */}
          <Badge variant="tag" className="max-w-[18rem]" title={chip(label)}>
            <span className="truncate">{chip(label)}</span>
          </Badge>
        </li>
      ))}
    </ul>
  )
}
