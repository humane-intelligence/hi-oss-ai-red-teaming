import { describe, expect, it } from 'vitest'
import { promptsBlockedReason } from './lifecycle'

describe('promptsBlockedReason', () => {
  it('lets anyone prompt in a published group', () => {
    expect(promptsBlockedReason('published', 'new', false)).toBeUndefined()
  })

  it('lets only whoever may edit the evaluation prompt before publication', () => {
    expect(promptsBlockedReason('approved', 'new', true)).toBeUndefined()
    expect(promptsBlockedReason('approved', 'new', false)).toMatch(/not published yet/)
    expect(promptsBlockedReason('draft', 'new', false)).toMatch(/not published yet/)
  })

  it('lets nobody prompt in a finished group or a closed evaluation', () => {
    expect(promptsBlockedReason('inactive', 'new', true)).toMatch(/finished/)
    expect(promptsBlockedReason('published', 'rejected', true)).toMatch(/closed/)
    expect(promptsBlockedReason('published', 'completed', true)).toMatch(/closed/)
  })

  it('gives no reason while a status is still unknown', () => {
    expect(promptsBlockedReason(undefined, 'new', false)).toBeUndefined()
    expect(promptsBlockedReason('inactive', undefined, false)).toBeUndefined()
  })
})
