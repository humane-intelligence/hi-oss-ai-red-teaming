import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { formatDate } from './format-date'

// West of UTC, where reading a bare date as UTC midnight would land on the day before.
beforeAll(() => {
  vi.stubEnv('TZ', 'America/Los_Angeles')
})
afterAll(() => {
  vi.unstubAllEnvs()
})

describe('formatDate', () => {
  it('formats a calendar date on that day', () => {
    expect(formatDate('2026-10-28')).toBe('Oct 28, 2026')
  })

  it('formats a timestamp in local time', () => {
    expect(formatDate('2026-01-06T05:30:00Z')).toBe('Jan 5, 2026')
  })
})
