import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SubmissionsTimelineChart } from './submissions-timeline-chart'
import type { DailySubmissionPoint } from '@/lib/api/types'

const points: DailySubmissionPoint[] = [
  { day: '2026-08-03', submissions: 3, exploited_submissions: 1 },
  { day: '2026-08-04', submissions: 0, exploited_submissions: 0 },
  { day: '2026-08-05', submissions: 1, exploited_submissions: 0 },
]

// Measured, not assumed: under jsdom `ResponsiveContainer` measures its parent to zero and renders
// no `<svg>` at all — it does not throw, but nothing inside the chart exists to assert. The visible
// caption is therefore the only surface this suite can check, and it doubles as the chart's
// accessible name, so both are asserted here.
describe('SubmissionsTimelineChart', () => {
  it('says nothing is plottable for an empty series', () => {
    render(<SubmissionsTimelineChart points={[]} />)
    expect(screen.getByText(/nothing to plot yet/i)).toBeInTheDocument()
    // No window and no totals to state, so no caption is rendered either.
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('states the window and the totals where a sighted reader can see them', () => {
    render(<SubmissionsTimelineChart points={points} />)
    expect(screen.getByText('3 Aug – 5 Aug · 4 submissions, 1 exploited')).toBeInTheDocument()
    // The Recharts legend keys the four series, so the caption adds no swatches of its own.
    expect(screen.queryAllByTestId('chart-caption-swatch')).toHaveLength(0)
  })

  it('takes its accessible name from that same caption', () => {
    render(<SubmissionsTimelineChart points={points} />)
    const chart = screen.getByRole('img')
    expect(chart).toHaveAccessibleName('3 Aug – 5 Aug · 4 submissions, 1 exploited')
  })

  it('keeps the totals when the series is bucketed past the column budget', () => {
    const many: DailySubmissionPoint[] = Array.from({ length: 40 }, (_, i) => {
      const day = new Date('2026-08-01T00:00:00Z')
      day.setUTCDate(day.getUTCDate() + i)
      return { day: day.toISOString().slice(0, 10), submissions: 2, exploited_submissions: 1 }
    })
    render(<SubmissionsTimelineChart points={many} maxColumns={10} />)
    const chart = screen.getByRole('img')
    // 40 days x 2 = 80, bucketed into 10 columns — the caption must still total the series.
    expect(chart).toHaveAccessibleName(/80 submissions/)
    expect(chart).toHaveAccessibleName(/40 exploited/)
  })
})

// Ends on a fixed day so the windowed captions are deterministic.
function series(days: number): DailySubmissionPoint[] {
  const last = new Date('2026-08-11T00:00:00Z')
  return Array.from({ length: days }, (_, i) => {
    const day = new Date(last)
    day.setUTCDate(last.getUTCDate() - (days - 1 - i))
    return { day: day.toISOString().slice(0, 10), submissions: 1, exploited_submissions: 0 }
  })
}

describe('SubmissionsTimelineChart — time range', () => {
  it('offers no range control for a series no longer than the shortest preset', () => {
    render(<SubmissionsTimelineChart points={points} />)
    expect(screen.queryByRole('radiogroup', { name: 'Time range' })).toBeNull()
    expect(screen.queryByText(/one bar per day|bars are/i)).toBeNull()
  })

  it('still offers nothing at exactly the shortest preset, where it would window nothing', () => {
    render(<SubmissionsTimelineChart points={series(7)} />)
    expect(screen.queryByRole('radiogroup', { name: 'Time range' })).toBeNull()
  })

  it('states the bucket width once a series is long enough to bucket', () => {
    render(<SubmissionsTimelineChart points={series(100)} />)
    // 100 days into at most 40 columns is ceil(100/40) = 3 days a bar.
    expect(screen.getByText('bars are 3 days')).toBeInTheDocument()
  })

  it('narrows the drawn window, its totals and its caption to the picked range', async () => {
    const user = userEvent.setup()
    render(<SubmissionsTimelineChart points={series(100)} />)
    expect(screen.getByRole('img')).toHaveAccessibleName(/100 submissions/)

    await user.click(screen.getByRole('radio', { name: '7 days' }))

    expect(
      screen.getByText('5 Aug – 11 Aug · 7 submissions, 0 exploited — in this range'),
    ).toBeInTheDocument()
    expect(screen.getByText('one bar per day')).toBeInTheDocument()
  })

  it('offers only the presets that would actually window the series', () => {
    render(<SubmissionsTimelineChart points={series(10)} />)
    // Ten days into a thirty- or ninety-day window is still ten days, so those two presets would
    // draw the same chart as All time — a control that does nothing.
    expect(screen.getAllByRole('radio').map((b) => b.textContent)).toEqual(['7 days', 'All time'])
  })

  it('offers every preset once the series outruns them all', () => {
    render(<SubmissionsTimelineChart points={series(100)} />)
    expect(screen.getAllByRole('radio').map((b) => b.textContent)).toEqual([
      '7 days',
      '30 days',
      '90 days',
      'All time',
    ])
  })

  it('falls back to the whole period when a shorter series drops the picked preset', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<SubmissionsTimelineChart points={series(100)} />)
    await user.click(screen.getByRole('radio', { name: '90 days' }))

    // Same chart, another group: 90 days is gone, and leaving it selected would leave the group
    // with nothing checked and so no tab stop at all.
    rerender(<SubmissionsTimelineChart points={series(10)} />)

    expect(screen.getByRole('radio', { name: 'All time' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('img')).toHaveAccessibleName(/10 submissions/)
    expect(screen.queryByText(/in this range/)).toBeNull()
  })

  // The element the chart is actually named by — a text match would land on the span inside it.
  const caption = () =>
    document.getElementById(screen.getByRole('img').getAttribute('aria-labelledby') ?? '')

  it('announces the re-windowed chart, whose accessible name would otherwise change in silence', () => {
    render(<SubmissionsTimelineChart points={series(100)} />)
    expect(caption()).toHaveAttribute('aria-live', 'polite')
  })

  it('stays silent where there is no control to change the window', () => {
    render(<SubmissionsTimelineChart points={points} />)
    // Only a refetch can move this caption, and nobody asked to hear about that.
    expect(caption()).not.toHaveAttribute('aria-live')
  })

  it('drops the range note when the reader goes back to the whole period', async () => {
    const user = userEvent.setup()
    render(<SubmissionsTimelineChart points={series(100)} />)
    await user.click(screen.getByRole('radio', { name: '7 days' }))
    await user.click(screen.getByRole('radio', { name: 'All time' }))
    expect(screen.getByRole('img')).toHaveAccessibleName(/100 submissions/)
    expect(screen.queryByText(/in this range/)).toBeNull()
  })
})
