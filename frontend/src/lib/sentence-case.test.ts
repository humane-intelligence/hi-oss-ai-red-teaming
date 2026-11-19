import { describe, expect, it } from 'vitest'
import { sentenceCase } from './sentence-case'

describe('sentenceCase', () => {
  it.each([
    ['pending_approval', 'Pending approval'],
    ['under_review', 'Under review'],
    ['invitation_only', 'Invitation only'],
    ['new', 'New'],
  ])('shows %s as %s', (value, shown) => {
    expect(sentenceCase(value)).toBe(shown)
  })
})
