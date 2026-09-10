import { describe, expect, it } from 'vitest'
import { tokenStore } from './token-store'

describe('tokenStore', () => {
  it('returns null when empty', () => {
    expect(tokenStore.get()).toBeNull()
  })
  it('persists and clears a token', () => {
    tokenStore.set('abc.def.ghi')
    expect(tokenStore.get()).toBe('abc.def.ghi')
    tokenStore.clear()
    expect(tokenStore.get()).toBeNull()
  })
})
