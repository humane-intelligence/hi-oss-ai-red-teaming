import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { ConversationGroupResponse } from '@/lib/api/types'

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
        <li key={label.category_id}>
          {/* Capped and titled because a free-text value runs to 200 chars. */}
          <Badge variant="tag" className="max-w-[18rem]" title={`${label.name}: ${label.value}`}>
            <span className="truncate">
              {label.name}: {label.value}
            </span>
          </Badge>
        </li>
      ))}
    </ul>
  )
}
