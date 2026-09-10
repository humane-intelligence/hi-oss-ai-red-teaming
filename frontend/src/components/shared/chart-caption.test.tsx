import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ChartCaption } from './chart-caption'

describe('ChartCaption', () => {
  it('states the window and the totals', () => {
    render(
      <ChartCaption
        id="c1"
        from="2026-07-23"
        to="2026-08-10"
        totals={{ submissions: 46, exploited: 13 }}
      />,
    )
    expect(screen.getByText('23 Jul – 10 Aug · 46 submissions, 13 exploited')).toBeInTheDocument()
  })

  it('collapses a one-day window to a single date', () => {
    render(
      <ChartCaption
        id="c2"
        from="2026-08-10"
        to="2026-08-10"
        totals={{ submissions: 4, exploited: 2 }}
      />,
    )
    expect(screen.getByText('10 Aug · 4 submissions, 2 exploited')).toBeInTheDocument()
  })

  // The axis caps at 366 days, so a year-long window is an ordinary one — without the year both
  // ends format identically and the caption reads as a single day.
  it('carries the year when the window spans two of them', () => {
    render(
      <ChartCaption
        id="c2b"
        from="2025-08-11"
        to="2026-08-11"
        totals={{ submissions: 1, exploited: 0 }}
      />,
    )
    expect(
      screen.getByText('11 Aug 2025 – 11 Aug 2026 · 1 submission, 0 exploited'),
    ).toBeInTheDocument()
  })

  it('appends a scope note when the caption describes a narrowed window', () => {
    render(
      <ChartCaption
        id="c6"
        from="2026-08-05"
        to="2026-08-11"
        totals={{ submissions: 7, exploited: 0 }}
        note="in this range"
      />,
    )
    expect(
      screen.getByText('5 Aug – 11 Aug · 7 submissions, 0 exploited — in this range'),
    ).toBeInTheDocument()
  })

  it('keys each drawn series with the colour the chart fills with', () => {
    render(
      <ChartCaption
        id="c3"
        series={[{ label: 'submissions', color: 'var(--color-muted-foreground)' }]}
        from="2026-08-03"
        to="2026-08-05"
        totals={{ submissions: 4, exploited: 1 }}
      />,
    )
    expect(screen.getByText('submissions')).toBeInTheDocument()
    const swatches = screen.getAllByTestId('chart-caption-swatch')
    expect(swatches).toHaveLength(1)
    expect(swatches[0]).toHaveStyle({ backgroundColor: 'var(--color-muted-foreground)' })
  })

  it('draws no swatches for a chart that keys its own series', () => {
    render(
      <ChartCaption
        id="c4"
        from="2026-08-03"
        to="2026-08-05"
        totals={{ submissions: 4, exploited: 1 }}
      />,
    )
    expect(screen.queryAllByTestId('chart-caption-swatch')).toHaveLength(0)
  })

  it('is a figcaption carrying the id a chart labels itself with', () => {
    render(
      <ChartCaption
        id="c5"
        from="2026-08-03"
        to="2026-08-05"
        totals={{ submissions: 4, exploited: 1 }}
      />,
    )
    const caption = document.getElementById('c5')
    expect(caption?.tagName).toBe('FIGCAPTION')
  })
})
