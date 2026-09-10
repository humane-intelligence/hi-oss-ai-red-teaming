import { describe, expect, it } from 'vitest'
import { mergeAssignablePools } from './queries'

describe('mergeAssignablePools', () => {
  it('unions reviewers across flags and dedupes shared ones', () => {
    const { options, poolByFlag } = mergeAssignablePools([
      {
        flagId: 'A',
        items: [
          { id: 'u1', email: 'u1@x', active_review_count: 0 },
          { id: 'u2', email: 'u2@x', active_review_count: 1 },
        ],
      },
      {
        flagId: 'B',
        items: [
          { id: 'u2', email: 'u2@x', active_review_count: 1 },
          { id: 'u3', email: 'u3@x', active_review_count: 2 },
        ],
      },
    ])
    expect(options.map((o) => o.value).sort()).toEqual(['u1', 'u2', 'u3'])
    expect(poolByFlag.get('A')).toEqual(new Set(['u1', 'u2']))
    expect(poolByFlag.get('B')).toEqual(new Set(['u2', 'u3']))
  })

  it('returns empty structures for no results', () => {
    const { options, poolByFlag } = mergeAssignablePools([])
    expect(options).toEqual([])
    expect(poolByFlag.size).toBe(0)
  })

  it('carries each reviewer’s open-work count into the option hint', () => {
    const { options } = mergeAssignablePools([
      { flagId: 'A', items: [{ id: 'u1', email: 'u1@x', active_review_count: 3 }] },
      { flagId: 'B', items: [{ id: 'u1', email: 'u1@x', active_review_count: 3 }] },
    ])
    expect(options).toEqual([{ value: 'u1', label: 'u1@x', hint: '3 active reviews' }])
  })

  it('says "1 active review", since the hint is read out loud beside the name', () => {
    const { options } = mergeAssignablePools([
      { flagId: 'A', items: [{ id: 'u1', email: 'u1@x', active_review_count: 1 }] },
    ])
    expect(options[0]?.hint).toBe('1 active review')
  })

  it('shows a zero rather than dropping the hint, so a missing badge is never ambiguous', () => {
    const { options } = mergeAssignablePools([
      { flagId: 'A', items: [{ id: 'u1', email: 'u1@x', active_review_count: 0 }] },
    ])
    expect(options[0]?.hint).toBe('0 active reviews')
  })
})
