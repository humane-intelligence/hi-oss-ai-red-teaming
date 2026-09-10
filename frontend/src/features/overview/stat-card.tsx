import { Link } from 'react-router-dom'
import type { ComponentType, ReactNode } from 'react'
import { Card } from '@/components/ui/card'

// Clickable persona stat: icon + label, a large live count, one-line context.
export function StatCard({
  to,
  icon: Icon,
  label,
  value,
  context,
}: {
  to: string
  icon: ComponentType<{ className?: string }>
  label: string
  value: ReactNode
  context: string
}) {
  return (
    <Link to={to} className="group">
      <Card className="group-hover:border-primary group-hover:bg-accent/40 h-full p-5 transition-colors">
        <div className="flex items-center gap-2">
          <Icon className="text-primary size-5" />
          <span className="font-medium">{label}</span>
        </div>
        <div className="font-display mt-2 text-3xl font-semibold tabular-nums">{value}</div>
        <p className="text-muted-foreground mt-1 text-sm">{context}</p>
      </Card>
    </Link>
  )
}
