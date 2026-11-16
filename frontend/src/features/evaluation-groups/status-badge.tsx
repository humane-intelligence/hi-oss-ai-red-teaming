import { LifecycleBadge } from '@/components/shared/lifecycle-badge'
import type { PublicationStatus } from '@/lib/api/types'

export function PublicationStatusBadge({ status }: { status: PublicationStatus }) {
  return <LifecycleBadge status={status} />
}
