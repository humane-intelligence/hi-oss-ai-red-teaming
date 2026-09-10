import { Gavel } from 'lucide-react'
import { useReviews } from '@/features/reviews/queries'
import { useAuth } from '@/lib/auth/auth-context'
import { StatCard } from './stat-card'

export function ReviewerPanel() {
  const { user } = useAuth()
  const q = useReviews({ limit: 1, offset: 0, status: 'pending', reviewer_id: user?.id })
  const n = q.data?.total
  return (
    <StatCard
      to="/reviews/mine"
      icon={Gavel}
      label="To review"
      value={n ?? '—'}
      context={n === 0 ? "You're all caught up." : 'Assigned to you, pending'}
    />
  )
}
