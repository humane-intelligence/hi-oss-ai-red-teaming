import { Globe } from 'lucide-react'
import { Badge } from '@/components/ui/badge'

// Shown wherever a reply is read: a model that may pull in live web content answers differently
// from one working off its training data, and the transcript itself does not say which it was.
export function WebAccessBadge() {
  return (
    <Badge variant="neutral" className="gap-1">
      <Globe className="size-3" /> Internet access
    </Badge>
  )
}
