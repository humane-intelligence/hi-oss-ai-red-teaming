import type { DailySubmissionPoint } from '@/lib/api/types'

export type TimelineBucket = {
  from: string
  to: string
  submissions: number
  exploited: number
}

export type CumulativeBucket = TimelineBucket & {
  cumulativeSubmissions: number
  cumulativeExploited: number
}

// Running totals across the buckets, oldest first. Daily counts answer "how busy was Tuesday";
// the cumulative answers "is this event accelerating, stalled, or over", which is the question the
// dashboard exists for and the one daily bars hide when most days are zero. The last bucket's
// running total is the series total, i.e. the number the roll-up card shows.
export function cumulative(buckets: TimelineBucket[]): CumulativeBucket[] {
  let submissions = 0
  let exploited = 0
  return buckets.map((bucket) => {
    submissions += bucket.submissions
    exploited += bucket.exploited
    return { ...bucket, cumulativeSubmissions: submissions, cumulativeExploited: exploited }
  })
}

// Place a sparse per-evaluation series onto a shared dense axis, zero-filling the gaps.
//
// The per-evaluation array carries only days that saw a submission, so drawing it directly would
// put a day and the day three days later side by side and read as consecutive. The group response's
// dense array is the axis those days belong to — the contract guarantees the sparse days are a
// subset of it — so densifying first makes each evaluation's curve true in time and comparable with
// its siblings. While there is an axis, a day somehow off it is appended rather than dropped, so a
// future contract change surfaces as an odd-looking chart instead of silently missing submissions;
// an empty axis is the one case that drops everything, because there is nothing to plot against.
export function densify(sparse: DailySubmissionPoint[], axis: string[]): DailySubmissionPoint[] {
  if (axis.length === 0) return []
  const byDay = new Map(sparse.map((point) => [point.day, point]))
  const onAxis = axis.map(
    (day) => byDay.get(day) ?? { day, submissions: 0, exploited_submissions: 0 },
  )
  // Set, not `axis.includes`: this runs once per evaluation on the group tab, and a linear scan
  // per sparse day makes it quadratic on a year-long axis.
  const axisDays = new Set(axis)
  const offAxis = sparse.filter((point) => !axisDays.has(point.day))
  return [...onAxis, ...offAxis]
}

// Sum consecutive days into at most `maxColumns` buckets, keeping the series total intact — the
// backend left the series uncapped so `sum(series) == submissions.total` holds, and the renderer
// must not be the thing that breaks it.
//
// The final bucket is short rather than padded: a short bucket is honest, a padded one invents days.
// Callers must label a bucket by its `from`/`to` range, never as a single day.
export function downsample(points: DailySubmissionPoint[], maxColumns: number): TimelineBucket[] {
  if (points.length === 0) return []
  const width = Math.ceil(points.length / maxColumns)
  const buckets: TimelineBucket[] = []
  for (let start = 0; start < points.length; start += width) {
    const slice = points.slice(start, start + width)
    const first = slice[0]!
    const last = slice[slice.length - 1]!
    buckets.push({
      from: first.day,
      to: last.day,
      submissions: slice.reduce((total, point) => total + point.submissions, 0),
      exploited: slice.reduce((total, point) => total + point.exploited_submissions, 0),
    })
  }
  return buckets
}
