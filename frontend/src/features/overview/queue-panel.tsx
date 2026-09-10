import { Inbox } from 'lucide-react'
import { useReviewQueue } from '@/features/reviews/queries'
import { StatCard } from './stat-card'

export function QueuePanel() {
  const q = useReviewQueue({ limit: 1, offset: 0 })
  const n = q.data?.total
  return (
    <StatCard
      to="/reviews/queue"
      icon={Inbox}
      label="Review queue"
      value={n ?? '—'}
      context={n === 0 ? 'Queue is clear.' : 'Submissions awaiting reviewers'}
    />
  )
}
