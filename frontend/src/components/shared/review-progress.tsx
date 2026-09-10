import { cn } from '@/lib/utils'

// Compact reviews-completed bar: "2/3" + "need 1", green once satisfied.
export function ReviewProgress({ completed, required }: { completed: number; required: number }) {
  const done = required > 0 && completed >= required
  const pct = required > 0 ? Math.min(100, Math.round((completed / required) * 100)) : 0
  return (
    <div className="w-28">
      <div className="text-muted-foreground flex items-center justify-between text-xs">
        <span className="tabular-nums">
          {completed}/{required}
        </span>
        {!done && required > completed && <span>need {required - completed}</span>}
      </div>
      <div className="bg-muted mt-1 h-1.5 overflow-hidden rounded-full">
        <div
          className={cn('h-full rounded-full', done ? 'bg-ok' : 'bg-primary')}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
