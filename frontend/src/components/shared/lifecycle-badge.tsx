import { sentenceCase } from '@/lib/sentence-case'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { EvaluationStatus, PublicationStatus } from '@/lib/api/types'

export type LifecycleStatus = PublicationStatus | EvaluationStatus

// One tone per stage, shared by groups and evaluations so a stage reads the same everywhere.
const toneByStatus: Record<LifecycleStatus, NonNullable<BadgeProps['variant']>> = {
  new: 'dashed',
  draft: 'neutral',
  pending_approval: 'warn',
  under_review: 'warn',
  changes_requested: 'changes',
  approved: 'approved',
  published: 'ok',
  not_approved: 'err',
  rejected: 'err',
  inactive: 'muted',
  completed: 'muted',
}

export function LifecycleBadge({ status }: { status: LifecycleStatus }) {
  return (
    <Badge variant={toneByStatus[status]} className="gap-1.5">
      {status === 'published' && (
        <span aria-hidden data-testid="published-dot" className="bg-ok size-1.5 rounded-full" />
      )}
      {sentenceCase(status)}
    </Badge>
  )
}
