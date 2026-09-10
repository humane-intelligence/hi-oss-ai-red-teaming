import { formatChartRange } from './chart-day'

export type CaptionSeries = { label: string; color: string }

// The visible description of a submissions chart: which series are drawn, the window, the totals.
// `series` is empty for a chart that keys its own colours (the timeline ships a Recharts `<Legend>`),
// and `color` is passed through to the swatch so the key cannot drift from the plotted fill.
export function ChartCaption({
  id,
  series = [],
  from,
  to,
  totals,
  note,
  live = false,
}: {
  id: string
  series?: CaptionSeries[]
  from: string
  to: string
  totals: { submissions: number; exploited: number }
  // Scopes the counts when the chart draws less than the whole series, so the caption never
  // contradicts the roll-up cards above it without saying why.
  note?: string
  // The caption is the chart's accessible name, and a name changing under `role="img"` is not
  // re-announced — so a re-windowable chart opts in. Every caption change then speaks, a refetch and
  // each arrow key included, which leans on `polite` coalescing (unverified on real AT).
  live?: boolean
}) {
  const window = formatChartRange(from, to)
  return (
    <figcaption
      id={id}
      aria-live={live ? 'polite' : undefined}
      className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
    >
      {/* The key is visual only. The accessible-name computation trims and concatenates its nodes, so
          leaving it in the name announces "submissions1 Aug" — and a colour key tells a screen-reader
          user nothing the summary below doesn't already state. */}
      {series.map((entry) => (
        <span key={entry.label} aria-hidden="true" className="inline-flex items-center gap-1.5">
          <span
            data-testid="chart-caption-swatch"
            className="size-2 rounded-[2px]"
            style={{ backgroundColor: entry.color }}
          />
          {entry.label}
        </span>
      ))}
      <span>{`${window} · ${totals.submissions} ${totals.submissions === 1 ? 'submission' : 'submissions'}, ${totals.exploited} exploited${note ? ` — ${note}` : ''}`}</span>
    </figcaption>
  )
}
