// The presets the timeline offers, shortest first. Their own module because both the control and
// the chart need them, and a component file cannot export a constant
// (`react-refresh/only-export-components`).
export type ChartRange = '7' | '30' | '90' | 'all'

export type ChartRangeOption = { value: ChartRange; label: string; days: number }

const RANGE_OPTIONS: ChartRangeOption[] = [
  { value: '7', label: '7 days', days: 7 },
  { value: '30', label: '30 days', days: 30 },
  { value: '90', label: '90 days', days: 90 },
]

const ALL_TIME: ChartRangeOption = {
  value: 'all',
  label: 'All time',
  days: Number.POSITIVE_INFINITY,
}

// A preset at least as long as the series draws the same chart as All time — a control that does
// nothing. All time left alone is the chart's cue to drop the control entirely.
export function rangeOptionsFor(seriesDays: number): ChartRangeOption[] {
  return [...RANGE_OPTIONS.filter((option) => option.days < seriesDays), ALL_TIME]
}
