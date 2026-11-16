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
    <ul aria-label="Labels" className={cn('flex flex-wrap gap-1.5', className)}>
      {labels.map((label) => (
        <li key={label.category_id}>
          <Badge variant="tag">
            {label.name}: {label.value}
          </Badge>
        </li>
      ))}
    </ul>
  )
}
