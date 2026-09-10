import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { PublicationStatus } from '@/lib/api/types'

const variantByStatus: Record<PublicationStatus, BadgeProps['variant']> = {
  draft: 'warn',
  pending_approval: 'warn',
  changes_requested: 'warn',
  approved: 'ok',
  not_approved: 'err',
  published: 'ok',
  inactive: 'err',
}

export function PublicationStatusBadge({ status }: { status: PublicationStatus }) {
  return <Badge variant={variantByStatus[status]}>{status.replace(/_/g, ' ')}</Badge>
}
