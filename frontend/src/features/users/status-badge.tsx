import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { UserStatus } from '@/lib/api/types'

const variantByStatus: Record<UserStatus, BadgeProps['variant']> = {
  active: 'ok',
  pending: 'warn',
  invited: 'warn',
  inactive: 'err',
}

// `pending` and `invited` share the `warn` colour, so a dot alone cannot tell them apart.
const shortByStatus: Record<UserStatus, string> = {
  active: 'ACT',
  pending: 'PEN',
  invited: 'INV',
  inactive: 'OFF',
}

// `compact` abbreviates rather than drops: the users list has no detail page to fall back on.
export function UserStatusBadge({ status, compact }: { status: UserStatus; compact?: boolean }) {
  if (!compact) return <Badge variant={variantByStatus[status]}>{status}</Badge>
  return (
    <Badge variant={variantByStatus[status]} aria-label={status}>
      <span aria-hidden className="sm:hidden">
        {shortByStatus[status]}
      </span>
      <span className="hidden sm:inline">{status}</span>
    </Badge>
  )
}
