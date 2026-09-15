import { Link } from 'react-router-dom'
import { Activity, Cpu, Flag, Gavel, Layers, Users } from 'lucide-react'
import type { ComponentType } from 'react'
import { PageHeader } from '@/components/shared/page-header'
import { Card } from '@/components/ui/card'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useAuth } from '@/lib/auth/auth-context'
import { ReviewerPanel } from './reviewer-panel'
import { RedTeamerPanel } from './red-teamer-panel'
import { QueuePanel } from './queue-panel'
import { GroupsApprovalPanel } from './groups-approval-panel'

type Section = {
  to: string
  title: string
  description: string
  icon: ComponentType<{ className?: string }>
  permsAny?: string[]
  rolesAny?: string[]
}

const sections: Section[] = [
  {
    to: '/evaluation-groups',
    title: 'Evaluation Groups',
    description: 'Engagements and the evaluations inside them.',
    icon: Layers,
    permsAny: ['evaluation_groups:read'],
  },
  {
    to: '/message-flags',
    title: 'My flags',
    description: 'Responses you flagged as exploit-worthy.',
    icon: Flag,
    permsAny: ['flags:read'],
  },
  {
    to: '/reviews',
    title: 'Reviews',
    description: 'Flagged submissions and reviewer verdicts.',
    icon: Gavel,
    permsAny: ['reviews:read'],
  },
  {
    to: '/ai-models',
    title: 'AI Models',
    description: 'Model registry available to the platform.',
    icon: Cpu,
    permsAny: ['models:read'],
  },
  {
    to: '/users',
    title: 'Users',
    description: 'Platform user accounts.',
    icon: Users,
    permsAny: ['users:read'],
  },
  {
    to: '/status',
    title: 'Backend status',
    description: 'Live readiness probe from the backend.',
    icon: Activity,
    rolesAny: ['admin'],
  },
]

export function OverviewPage() {
  const { has, hasRole } = usePermissions()
  const { user } = useAuth()

  const panels = [
    has('reviews:update') && <ReviewerPanel key="reviewer" />,
    has('flags:create') && <RedTeamerPanel key="red-teamer" />,
    has('reviews:create') && <QueuePanel key="queue" />,
    has('evaluation_groups:update') && <GroupsApprovalPanel key="groups" />,
  ].filter(Boolean)

  const visible = sections.filter(
    (s) =>
      (!s.permsAny || s.permsAny.some((p) => has(p))) &&
      (!s.rolesAny || s.rolesAny.some((r) => hasRole(r))),
  )

  const name = user?.first_name || user?.email

  return (
    <div className="space-y-8">
      <PageHeader
        title={name ? `Welcome back, ${name}` : 'Overview'}
        description="Here's what needs your attention."
      />

      {panels.length > 0 && (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {panels}
        </div>
      )}

      <div className="space-y-3">
        <h2 className="text-muted-foreground font-mono text-[10px]">All sections</h2>
        <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {visible.map(({ to, title, description, icon: Icon }) => (
            <Link key={to} to={to} className="group">
              <Card className="group-hover:border-primary group-hover:bg-accent/40 h-full p-5 transition-colors">
                <div className="flex items-center gap-2">
                  <Icon className="text-primary size-5" />
                  <span className="font-medium">{title}</span>
                </div>
                <p className="text-muted-foreground mt-2 text-sm">{description}</p>
              </Card>
            </Link>
          ))}
        </div>
      </div>
    </div>
  )
}
