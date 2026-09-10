import { Flag } from 'lucide-react'
import { useMessageFlags } from '@/features/message-flags/queries'
import { StatCard } from './stat-card'

export function RedTeamerPanel() {
  const all = useMessageFlags({ limit: 1, offset: 0 })
  const pending = useMessageFlags({ limit: 1, offset: 0, status: 'pending' })
  const total = all.data?.total
  const pend = pending.data?.total
  return (
    <StatCard
      to="/message-flags"
      icon={Flag}
      label="Your flags"
      value={total ?? '—'}
      context={pend ? `${pend} awaiting verdict` : 'Responses you flagged'}
    />
  )
}
