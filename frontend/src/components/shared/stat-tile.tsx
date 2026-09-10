import type { ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'

// At-a-glance telemetry tile: mono micro-label over a large value. With `to` the whole tile is the
// link, so the number and the way to act on it are the same target; the chevron marks it as one.
export function StatTile({ label, value, to }: { label: string; value: ReactNode; to?: string }) {
  const body = (
    <>
      {/* A long single-word label used to spill past the card; the value div stays the next sibling. */}
      <div className="text-muted-foreground flex min-w-0 items-center justify-between gap-2 font-mono text-[10px] tracking-[0.2em] wrap-anywhere uppercase">
        {label}
        {to != null && <ChevronRight className="size-4 shrink-0" aria-hidden />}
      </div>
      <div className="font-display mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </>
  )
  const shell = 'bg-card block rounded-lg border px-4 py-3'
  return to ? (
    <Link to={to} className={cn(shell, 'hover:border-primary/60 transition-colors')}>
      {body}
    </Link>
  ) : (
    <div className={shell}>{body}</div>
  )
}
