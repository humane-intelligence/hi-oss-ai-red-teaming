import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  TOOLTIP_CONTENT_STYLE,
  TOOLTIP_ITEM_STYLE,
  TOOLTIP_LABEL_STYLE,
} from './chart-tooltip-style'
import type { CumulativeBucket } from './downsample'

export type ChartBucket = CumulativeBucket & { label: string; other: number }

// The Recharts half, split out so it can be loaded lazily: measured, it weighs ~357 kB raw /
// ~103 kB gzip, and both detail pages would otherwise pay it whether or not the operator ever
// opens the Metrics tab.
//
// Two readings in one chart. The stacked bars are the daily texture — `exploited` on top of
// `other` (= submissions - exploited), so a column's height is `submissions`; stacking the two API
// fields directly would draw their sum and overstate every day. The lines are the running totals on
// their own axis, because that is the question the dashboard exists for ("accelerating, stalled or
// over") and daily bars hide it whenever most days are zero.
//
// Four marks need naming, hence the legend: at 260 px tall an unlabelled quartet is unreadable.
export function SubmissionsTimelineRecharts({ buckets }: { buckets: ChartBucket[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <ComposedChart data={buckets}>
        <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
        <YAxis yAxisId="daily" allowDecimals={false} tick={{ fontSize: 11 }} width={32} />
        <YAxis
          yAxisId="total"
          orientation="right"
          allowDecimals={false}
          tick={{ fontSize: 11 }}
          width={32}
        />
        <Tooltip
          separator=": "
          contentStyle={TOOLTIP_CONTENT_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {/* Not `--color-primary`: `--color-ring` resolves to the same accent and draws the total
            line below, so the stack and that line would share a hue. */}
        <Bar
          yAxisId="daily"
          dataKey="other"
          stackId="day"
          fill="var(--color-muted-foreground)"
          name="not exploited"
        />
        <Bar
          yAxisId="daily"
          dataKey="exploited"
          stackId="day"
          fill="var(--color-warn)"
          name="exploited"
        />
        <Line
          yAxisId="total"
          type="monotone"
          dataKey="cumulativeSubmissions"
          stroke="var(--color-ring)"
          strokeWidth={2}
          dot={false}
          name="total submissions"
        />
        <Line
          yAxisId="total"
          type="monotone"
          dataKey="cumulativeExploited"
          stroke="var(--color-warn)"
          strokeWidth={2}
          strokeDasharray="4 3"
          dot={false}
          name="total exploited"
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
