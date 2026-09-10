import { InfoHint } from '@/components/shared/info-hint'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

// A labelled magnitude bar list — one row per category, a recessive track with a proportional
// fill and the count shown directly. Single series, so no legend; bars read left-to-right.
// Rows carry an explicit `key` since labels can repeat (a masked model with no alias set).
//
// `emptyLabel` is deliberately a caller's string rather than a fixed "Nothing to show": an empty
// list can mean different things per chart, and one of them (attribution withheld under masking)
// must not read as "there is nothing here".
export function BarList({
  title,
  hint,
  rows,
  emptyLabel,
}: {
  title: string
  hint?: string
  rows: { key: string; label: string; value: number }[]
  emptyLabel: string
}) {
  const max = Math.max(1, ...rows.map((r) => r.value))
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          {title}
          {hint && <InfoHint text={hint} />}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {rows.length === 0 ? (
          <p className="text-muted-foreground text-sm">{emptyLabel}</p>
        ) : (
          rows.map((row) => (
            <div key={row.key} className="space-y-1">
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="text-muted-foreground min-w-0 truncate">{row.label}</span>
                <span className="font-medium tabular-nums">{row.value.toLocaleString()}</span>
              </div>
              <div className="bg-muted h-2 rounded-full">
                <div
                  className="bg-primary h-2 rounded-full"
                  style={{ width: `${(row.value / max) * 100}%` }}
                />
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  )
}
