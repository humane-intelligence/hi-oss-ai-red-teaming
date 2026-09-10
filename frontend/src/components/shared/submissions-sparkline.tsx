import { Suspense, lazy, useId } from 'react'
import { densify } from './downsample'
import { ChartCaption } from './chart-caption'
import type { DailySubmissionPoint } from '@/lib/api/types'

const SubmissionsSparklineRecharts = lazy(() =>
  import('./submissions-sparkline-recharts').then((m) => ({
    default: m.SubmissionsSparklineRecharts,
  })),
)

// A per-evaluation curve on the group's own dense axis. The API sends each evaluation only the days
// that carry a submission, so drawing that array directly would put a day and the day three days
// later side by side and read as consecutive; the contract guarantees those days are a subset of the
// group's dense axis, so densifying against it makes the curve true in time and comparable between
// evaluations. Recharts stays behind the same lazy boundary as the big chart.
export function SubmissionsSparkline({
  sparse,
  axis,
}: {
  sparse: DailySubmissionPoint[]
  axis: string[]
}) {
  // One id per instance: the group tab renders one of these per evaluation on a single page.
  const captionId = useId()
  const points = densify(sparse, axis)
  const total = points.reduce((sum, point) => sum + point.submissions, 0)
  if (points.length === 0 || total === 0) {
    return <p className="text-muted-foreground text-xs">No submissions yet.</p>
  }
  const exploited = points.reduce((sum, point) => sum + point.exploited_submissions, 0)
  return (
    <figure className="space-y-1.5">
      <div role="img" aria-labelledby={captionId}>
        <Suspense fallback={<div className="h-10" />}>
          <SubmissionsSparklineRecharts points={points} />
        </Suspense>
      </div>
      {/* One swatch, in the chart's own fill token: this chart draws `submissions` only. The
          exploited count still belongs in the totals — it is a fact about the data, not a key to a
          band on the picture. */}
      <ChartCaption
        id={captionId}
        series={[{ label: 'submissions', color: 'var(--color-muted-foreground)' }]}
        from={points[0]!.day}
        to={points[points.length - 1]!.day}
        totals={{ submissions: total, exploited }}
      />
    </figure>
  )
}
