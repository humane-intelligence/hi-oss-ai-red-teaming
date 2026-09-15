import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { EvaluationStatus } from '@/lib/api/types'

const variantByStatus: Record<EvaluationStatus, BadgeProps['variant']> = {
  new: 'new',
  draft: 'warn',
  under_review: 'warn',
  rejected: 'err',
  approved: 'ok',
  published: 'ok',
  completed: 'neutral',
}

export function EvaluationStatusBadge({ status }: { status: EvaluationStatus }) {
  return <Badge variant={variantByStatus[status]}>{status.replace('_', ' ')}</Badge>
}
