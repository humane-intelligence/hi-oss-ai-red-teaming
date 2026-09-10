// Formatted in UTC: `day` is a UTC calendar day from the API, so rendering it in a zone west of UTC
// would print the previous date. The locale is pinned rather than taken from the viewer because this
// string reaches the accessible name of a chart and is the only surface a jsdom test can assert — a
// viewer-dependent day/month order would make that assertion locale-dependent.
//
// Its own module rather than a second export from `chart-caption.tsx`: `react-refresh/only-export-components`
// fails a component file that also exports a function.
const DAY_FORMAT = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  timeZone: 'UTC',
})

const DAY_YEAR_FORMAT = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
})

export function formatChartDay(day: string): string {
  return DAY_FORMAT.format(new Date(`${day}T00:00:00Z`))
}

// The axis has no upper bound — the contract caps only the empty lead-in — so without the year a
// multi-year window renders its two ends as one string ("11 Aug – 11 Aug"), and any window across
// New Year reads as though it ran backwards.
export function formatChartRange(from: string, to: string): string {
  if (from === to) return formatChartDay(from)
  const format =
    from.slice(0, 4) === to.slice(0, 4)
      ? formatChartDay
      : (day: string) => DAY_YEAR_FORMAT.format(new Date(`${day}T00:00:00Z`))
  return `${format(from)} – ${format(to)}`
}
