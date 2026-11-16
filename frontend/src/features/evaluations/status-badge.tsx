import { LifecycleBadge } from '@/components/shared/lifecycle-badge'
import type { EvaluationStatus } from '@/lib/api/types'

export function EvaluationStatusBadge({ status }: { status: EvaluationStatus }) {
  return <LifecycleBadge status={status} />
}
