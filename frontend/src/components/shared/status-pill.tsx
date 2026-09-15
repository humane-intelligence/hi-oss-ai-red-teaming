import { Badge, type BadgeProps } from '@/components/ui/badge'

const toneByStatus: Record<string, BadgeProps['variant']> = {
  active: 'ok',
  approved: 'ok',
  accepted: 'ok',
  published: 'ok',
  resolved: 'ok',
  pending: 'warn',
  pending_approval: 'warn',
  under_review: 'warn',
  in_review: 'warn',
  changes_requested: 'warn',
  draft: 'warn',
  invited: 'warn',
  new: 'new',
  open: 'warn',
  rejected: 'err',
  not_approved: 'err',
  inactive: 'err',
  expired: 'err',
  revoked: 'err',
  dismissed: 'err',
  failed: 'err',
  red_flagged: 'err',
  completed: 'neutral',
}

export function StatusPill({ status }: { status: string }) {
  return <Badge variant={toneByStatus[status] ?? 'neutral'}>{status.replace(/_/g, ' ')}</Badge>
}
