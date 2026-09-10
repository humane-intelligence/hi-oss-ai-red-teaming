import { Layers } from 'lucide-react'
import { useEvaluationGroups } from '@/features/evaluation-groups/queries'
import { StatCard } from './stat-card'

export function GroupsApprovalPanel() {
  const q = useEvaluationGroups({ limit: 1, offset: 0, status: 'pending_approval' })
  const n = q.data?.total
  return (
    <StatCard
      to="/evaluation-groups"
      icon={Layers}
      label="Groups to approve"
      value={n ?? '—'}
      context={n === 0 ? 'Nothing awaiting approval.' : 'Awaiting your approval'}
    />
  )
}
