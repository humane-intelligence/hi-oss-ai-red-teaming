import { describe, expect, it } from 'vitest'
import { cumulative, densify, downsample } from './downsample'
import type { DailySubmissionPoint } from '@/lib/api/types'

// Real consecutive dates, not `2026-08-${i}` — the function treats a day as an opaque string, so a
// fixture with `2026-08-400` in it would still pass while lying about what the data looks like.
const series = (n: number, from = '2026-08-01'): DailySubmissionPoint[] =>
  Array.from({ length: n }, (_, i) => {
    const day = new Date(`${from}T00:00:00Z`)
    day.setUTCDate(day.getUTCDate() + i)
    return {
      day: day.toISOString().slice(0, 10),
      submissions: i + 1,
      exploited_submissions: i % 2,
    }
  })

const sum = (ns: number[]) => ns.reduce((a, b) => a + b, 0)

describe('downsample', () => {
  it('passes a series shorter than the budget through unchanged', () => {
    const out = downsample(series(3), 10)
    expect(out).toHaveLength(3)
    expect(out[0]).toEqual({ from: '2026-08-01', to: '2026-08-01', submissions: 1, exploited: 0 })
  })

  it('returns nothing for an empty series', () => {
    expect(downsample([], 10)).toEqual([])
  })

  it('handles a single point', () => {
    expect(downsample(series(1), 10)).toHaveLength(1)
  })

  it('buckets at the boundary: 11 points into a budget of 10 gives 6 buckets of width 2', () => {
    const out = downsample(series(11), 10)
    expect(out).toHaveLength(6)
    expect(out[0]).toEqual({ from: '2026-08-01', to: '2026-08-02', submissions: 3, exploited: 1 })
  })

  it('leaves the last bucket short rather than padding it', () => {
    const out = downsample(series(11), 10)
    const last = out[out.length - 1]
    // 11 points, width 2 -> the final bucket holds one day. Padding would invent a day.
    expect(last).toEqual({ from: '2026-08-11', to: '2026-08-11', submissions: 11, exploited: 0 })
  })

  it('preserves the totals — the invariant the backend refuses to break', () => {
    const points = series(400)
    const out = downsample(points, 60)
    expect(out.length).toBeLessThanOrEqual(60)
    expect(sum(out.map((b) => b.submissions))).toBe(sum(points.map((p) => p.submissions)))
    expect(sum(out.map((b) => b.exploited))).toBe(sum(points.map((p) => p.exploited_submissions)))
  })

  it('covers every day exactly once, in order', () => {
    const points = series(37)
    const out = downsample(points, 10)
    expect(out[0]!.from).toBe(points[0]!.day)
    expect(out[out.length - 1]!.to).toBe(points[points.length - 1]!.day)
    for (let i = 1; i < out.length; i += 1) expect(out[i]!.from > out[i - 1]!.to).toBe(true)
  })
})

describe('cumulative', () => {
  const buckets = [
    { from: '2026-08-01', to: '2026-08-01', submissions: 3, exploited: 1 },
    { from: '2026-08-02', to: '2026-08-02', submissions: 0, exploited: 0 },
    { from: '2026-08-03', to: '2026-08-03', submissions: 2, exploited: 2 },
  ]

  it('runs a total across the buckets in order', () => {
    expect(cumulative(buckets).map((b) => b.cumulativeSubmissions)).toEqual([3, 3, 5])
    expect(cumulative(buckets).map((b) => b.cumulativeExploited)).toEqual([1, 1, 3])
  })

  it('ends on the series total — the number the roll-up card shows', () => {
    const out = cumulative(buckets)
    const last = out[out.length - 1]!
    expect(last.cumulativeSubmissions).toBe(5)
    expect(last.cumulativeExploited).toBe(3)
  })

  it('returns nothing for no buckets', () => {
    expect(cumulative([])).toEqual([])
  })

  it('never decreases', () => {
    const out = cumulative(buckets)
    for (let i = 1; i < out.length; i += 1) {
      expect(out[i]!.cumulativeSubmissions).toBeGreaterThanOrEqual(
        out[i - 1]!.cumulativeSubmissions,
      )
    }
  })
})

describe('densify', () => {
  const axis = ['2026-08-01', '2026-08-02', '2026-08-03', '2026-08-04']
  // Sparse: only the days that carry a submission, which is what the API sends per evaluation.
  const sparse: DailySubmissionPoint[] = [
    { day: '2026-08-01', submissions: 3, exploited_submissions: 1 },
    { day: '2026-08-04', submissions: 2, exploited_submissions: 0 },
  ]

  it('places each sparse day on the axis and zero-fills the gaps', () => {
    expect(densify(sparse, axis)).toEqual([
      { day: '2026-08-01', submissions: 3, exploited_submissions: 1 },
      { day: '2026-08-02', submissions: 0, exploited_submissions: 0 },
      { day: '2026-08-03', submissions: 0, exploited_submissions: 0 },
      { day: '2026-08-04', submissions: 2, exploited_submissions: 0 },
    ])
  })

  it('preserves the totals and the axis length', () => {
    const out = densify(sparse, axis)
    expect(out).toHaveLength(axis.length)
    expect(sum(out.map((p) => p.submissions))).toBe(sum(sparse.map((p) => p.submissions)))
  })

  it('drops nothing when the sparse day is missing from the axis', () => {
    // The contract guarantees sparse ⊆ dense, so this is defence against a future contract change
    // rather than a case the API produces: a day off the axis must not vanish silently.
    const out = densify([{ day: '2026-09-09', submissions: 7, exploited_submissions: 7 }], axis)
    expect(sum(out.map((p) => p.submissions))).toBe(7)
  })

  it('returns an empty axis unchanged', () => {
    expect(densify(sparse, [])).toEqual([])
  })
})
