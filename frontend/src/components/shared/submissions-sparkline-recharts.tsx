import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { formatChartDay } from './chart-day'
import {
  TOOLTIP_CONTENT_STYLE,
  TOOLTIP_ITEM_STYLE,
  TOOLTIP_LABEL_STYLE,
} from './chart-tooltip-style'
import type { DailySubmissionPoint } from '@/lib/api/types'

// Deliberately axis-free: at ~40 px tall a sparkline carries shape, and the numbers live beside it in
// the breakdown and in the caption below. Same colour as the big chart's neutral segment, so the two
// read as one system.
//
// `linear`, not `monotone`: these are discrete daily counts, and a monotone spline smooths them into
// a sine and overshoots between points, so a run of quiet days turns into a rolling wave that the
// data does not contain. The domain is pinned to `[0, dataMax]` with the area based at 0 so a zero
// day visibly sits on the floor — with an auto domain the baseline floats and days with no
// submissions look like small ones.
export function SubmissionsSparklineRecharts({ points }: { points: DailySubmissionPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={40}>
      <AreaChart data={points} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        {/* Hidden, but required: the tooltip takes its label from the x-axis `dataKey`, and without
            one Recharts labels every point with its array index instead of the day. */}
        <XAxis dataKey="day" hide />
        <YAxis hide domain={[0, 'dataMax']} />
        <Tooltip
          labelFormatter={(day) => formatChartDay(String(day))}
          // Recharts hands the formatter a `ValueType | undefined`; this chart's only series is a count.
          formatter={(value) => [value as number, 'submissions'] as [number, string]}
          // Recharts' default separator is " : ", which prints a space before the colon.
          separator=": "
          wrapperStyle={{ fontSize: 11 }}
          contentStyle={TOOLTIP_CONTENT_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
        />
        <Area
          type="linear"
          dataKey="submissions"
          baseValue={0}
          stroke="var(--color-muted-foreground)"
          fill="var(--color-muted-foreground)"
          fillOpacity={0.25}
          strokeWidth={1.5}
          dot={false}
          // The hover target: with `dot={false}` and no active dot there is nothing on the curve to
          // aim at, which is why this chart read as inert before.
          activeDot={{ r: 2.5, strokeWidth: 0 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
