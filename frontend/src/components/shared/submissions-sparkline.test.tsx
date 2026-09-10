import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SubmissionsSparkline } from './submissions-sparkline'
import type { DailySubmissionPoint } from '@/lib/api/types'

const axis = ['2026-08-01', '2026-08-02', '2026-08-03', '2026-08-04']
const sparse: DailySubmissionPoint[] = [
  { day: '2026-08-01', submissions: 3, exploited_submissions: 1 },
  { day: '2026-08-04', submissions: 2, exploited_submissions: 0 },
]

describe('SubmissionsSparkline', () => {
  it('states the group window and its own totals, visibly', () => {
    render(<SubmissionsSparkline sparse={sparse} axis={axis} />)
    expect(screen.getByText('1 Aug – 4 Aug · 5 submissions, 1 exploited')).toBeInTheDocument()
    // The colour key stays out of the name — it would be concatenated as "submissions1 Aug", and it
    // says nothing the summary doesn't. Both charts therefore announce the same shape of sentence.
    expect(screen.getByRole('img')).toHaveAccessibleName(
      '1 Aug – 4 Aug · 5 submissions, 1 exploited',
    )
  })

  it('keys the one series it actually draws, and only that one', () => {
    render(<SubmissionsSparkline sparse={sparse} axis={axis} />)
    // The chart plots `submissions` alone — an `exploited` swatch here would advertise a series
    // that is not on the picture, even though the totals mention it.
    const swatches = screen.getAllByTestId('chart-caption-swatch')
    expect(swatches).toHaveLength(1)
    expect(swatches[0]).toHaveStyle({ backgroundColor: 'var(--color-muted-foreground)' })
    expect(screen.getByText('submissions')).toBeInTheDocument()
  })

  it('says so when the evaluation has no submissions at all', () => {
    render(<SubmissionsSparkline sparse={[]} axis={axis} />)
    expect(screen.getByText(/no submissions yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('renders nothing chart-like when the group axis is empty', () => {
    // A group with nothing to plot has an empty dense axis; an evaluation cannot have a curve then.
    render(<SubmissionsSparkline sparse={sparse} axis={[]} />)
    expect(screen.getByText(/no submissions yet/i)).toBeInTheDocument()
  })
})
