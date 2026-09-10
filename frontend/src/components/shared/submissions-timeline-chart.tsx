import { Suspense, lazy, useId, useState } from 'react'
import { cumulative, downsample } from './downsample'
import { ChartCaption } from './chart-caption'
import { formatChartRange } from './chart-day'
import { ChartRangePicker } from './chart-range-picker'
import { rangeOptionsFor, type ChartRange } from './chart-range-ranges'
import type { DailySubmissionPoint } from '@/lib/api/types'

// Recharts loads only once a chart is actually shown. Measured: it adds ~357 kB raw / ~103 kB gzip
// to a chunk both detail pages import, so eagerly bundling it would charge every viewer of either
// page for a tab most of them never open. The empty state and the accessible summary stay in this
// module, synchronous — they are the cheap, always-needed half, and the summary is also the only
// surface a jsdom test can assert (`ResponsiveContainer` measures its parent to zero there and
// renders no `<svg>` at all).
const SubmissionsTimelineRecharts = lazy(() =>
  import('./submissions-timeline-recharts').then((m) => ({
    default: m.SubmissionsTimelineRecharts,
  })),
)

// Set from the browser pass, not chosen: measured at a 375 px viewport the chart is 293 px wide and
// 11 columns render as 18 px bars, i.e. a pitch of ~20 px across ~220 px of plot area. 40 columns
// therefore leave ~5.5 px per column — still enough for the exploited segment to be a visible band
// inside its column — where 60 would leave ~3.7 px and reduce it to a hairline. (The 18 px figure is
// measured; the per-column arithmetic that follows from it is not.)
const MAX_COLUMNS = 40

export function SubmissionsTimelineChart({
  points,
  maxColumns = MAX_COLUMNS,
}: {
  points: DailySubmissionPoint[]
  maxColumns?: number
}) {
  // One id per instance: the evaluation dashboard renders one of these, the group tab renders one
  // more alongside a sparkline per evaluation, and each chart must point at its own caption.
  const captionId = useId()
  const [picked, setPicked] = useState<ChartRange>('all')
  if (points.length === 0) {
    // The contract names the two causes, so say them rather than leaving the reader to guess
    // whether the chart is broken.
    return (
      <p className="text-muted-foreground text-sm">
        Nothing to plot yet — no submissions, and no start date has been reached.
      </p>
    )
  }

  const rangeOptions = rangeOptionsFor(points.length)
  // Derived, not an effect: the same chart can be handed a shorter series (another group, a refetch)
  // that no longer offers the picked preset, which would otherwise leave the group with nothing
  // checked and so no tab stop at all.
  const range = rangeOptions.some((option) => option.value === picked) ? picked : 'all'
  // The series is dense, contiguous and oldest-first, so a window is its tail.
  const shown = range === 'all' ? points : points.slice(-Number(range))
  const buckets = cumulative(downsample(shown, maxColumns)).map((bucket) => ({
    ...bucket,
    // A bucket spanning several days must never read as one day. Same formatter as the caption
    // below, so one card never shows two date formats — and the short form is what keeps a
    // 40-bucket axis from clipping its ticks. On an axis spanning years each bucket still sits
    // inside one of them, so ticks stay year-less and the caption is what carries the span.
    label: formatChartRange(bucket.from, bucket.to),
    other: bucket.submissions - bucket.exploited,
  }))
  const first = shown[0]!
  const last = shown[shown.length - 1]!
  const totalSubmissions = buckets.reduce((total, bucket) => total + bucket.submissions, 0)
  const totalExploited = buckets.reduce((total, bucket) => total + bucket.exploited, 0)
  // One option left means every preset would draw the same chart — no control worth showing.
  const offerRange = rangeOptions.length > 1
  // Same arithmetic `downsample` uses, so the label cannot drift from the bars it describes.
  const bucketDays = Math.ceil(shown.length / maxColumns)

  return (
    <figure className="space-y-2">
      {offerRange && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-muted-foreground text-xs">
            {bucketDays === 1 ? 'one bar per day' : `bars are ${bucketDays} days`}
          </span>
          <ChartRangePicker value={range} options={rangeOptions} onChange={setPicked} />
        </div>
      )}
      {/* The caption is the chart's accessible name rather than a second copy of the same facts in an
          `aria-label` — one source, one announcement. No swatches: the chart ships a Recharts
          `<Legend>` that keys all four series. */}
      <div role="img" aria-labelledby={captionId}>
        <Suspense fallback={<div className="h-[260px]" />}>
          <SubmissionsTimelineRecharts buckets={buckets} />
        </Suspense>
      </div>
      <ChartCaption
        id={captionId}
        live={offerRange}
        from={first.day}
        to={last.day}
        totals={{ submissions: totalSubmissions, exploited: totalExploited }}
        // Keyed on what was actually cut, not on what was picked: a 30-day preset over a 10-day
        // series draws the whole thing, and a caption claiming a narrowed window would be false.
        note={shown.length < points.length ? 'in this range' : undefined}
      />
    </figure>
  )
}
