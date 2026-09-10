import { Navigate, NavLink, Outlet, useMatch } from 'react-router-dom'
import { PageHeader } from '@/components/shared/page-header'
import { StatTile } from '@/components/shared/stat-tile'
import { useReviewQueue, useReviews } from './queries'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import { cn } from '@/lib/utils'

function ReviewsStats() {
  const { user } = useAuth()
  const { has } = usePermissions()
  const isReviewer = has('reviews:update')
  const queue = useReviewQueue({ limit: 1, offset: 0 })
  const unassigned = useReviewQueue({ limit: 1, offset: 0, unassigned: true })
  const mine = useReviews({ limit: 100, offset: 0, reviewer_id: isReviewer ? user?.id : undefined })
  const myReviews = isReviewer ? (mine.data?.items ?? []) : []
  const myPending = myReviews.filter((r) => r.status === 'pending').length
  return (
    <div className={cn('grid gap-3', isReviewer ? 'grid-cols-2 lg:grid-cols-4' : 'grid-cols-2')}>
      <StatTile
        label={isReviewer ? 'Awaiting review' : 'Awaiting review (yours)'}
        value={queue.data?.total ?? 0}
      />
      <StatTile
        label={isReviewer ? 'Unassigned' : 'Unassigned (yours)'}
        // Not `?? 0`: this tile is a link, and a zero you can click into a list of four is worse
        // than an em dash that says the number has not arrived.
        value={unassigned.data ? unassigned.data.total : '—'}
        to="/reviews/queue?unassigned=1"
      />
      {isReviewer && <StatTile label="Assigned to me" value={myPending} />}
      {isReviewer && <StatTile label="Reviewed by me" value={myReviews.length - myPending} />}
    </div>
  )
}

type ReviewTabDef = { to: string; label: string; show: boolean }

function ReviewTab({ to, label }: { to: string; label: string }) {
  const active = useMatch(to) != null
  return (
    <NavLink
      to={to}
      role="tab"
      aria-selected={active}
      className={cn(
        'border-b-2 px-3 py-2 text-sm font-medium transition-colors',
        active
          ? 'border-primary text-foreground'
          : 'text-muted-foreground hover:text-foreground border-transparent',
      )}
    >
      {label}
    </NavLink>
  )
}

// Default landing: a reviewer (can record verdicts) starts on their own worklist;
// a read-only red-teamer has none, so lands on the queue (BE-scoped to their flags).
export function ReviewsIndexRedirect() {
  const { has } = usePermissions()
  return <Navigate to={has('reviews:update') ? '/reviews/mine' : '/reviews/queue'} replace />
}

export function ReviewsLayout() {
  const { has } = usePermissions()
  const tabs: ReviewTabDef[] = [
    { to: '/reviews/mine', label: 'My reviews', show: has('reviews:update') },
    { to: '/reviews/queue', label: 'Queue', show: true },
    { to: '/reviews/all', label: 'All reviews', show: true },
  ]
  return (
    <div className="space-y-6">
      <PageHeader title="Reviews" description="Flagged submissions and reviewer verdicts." />
      <ReviewsStats />
      <div role="tablist" className="flex gap-1 border-b">
        {tabs
          .filter((t) => t.show)
          .map((t) => (
            <ReviewTab key={t.to} to={t.to} label={t.label} />
          ))}
      </div>
      <Outlet />
    </div>
  )
}
