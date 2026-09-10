import { afterEach, describe, expect, it, vi } from 'vitest'

import { uuid } from './uuid'

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

// An insecure origin exposes no `randomUUID` at all. It lives on Crypto.prototype, so hide it
// behind an own property rather than deleting it, and drop that shadow again afterwards.
const hideRandomUUID = () =>
  Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true })

describe('uuid', () => {
  afterEach(() => {
    Reflect.deleteProperty(crypto, 'randomUUID')
    vi.restoreAllMocks()
  })

  it('returns the native uuid when randomUUID is available', () => {
    const native = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(native)
    expect(uuid()).toBe(native)
  })

  it('falls back to getRandomValues when randomUUID is missing', () => {
    hideRandomUUID()
    const getRandomValues = vi.spyOn(crypto, 'getRandomValues')
    expect(uuid()).toMatch(V4)
    expect(getRandomValues).toHaveBeenCalled()
  })

  it('sets the version and variant bits on the fallback path', () => {
    hideRandomUUID()
    // A fresh Uint8Array is all zeros, so only the bits uuid() forces show up.
    vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => array)
    expect(uuid()).toBe('00000000-0000-4000-8000-000000000000')
  })

  it('does not repeat across many fallback draws', () => {
    hideRandomUUID()
    const seen = new Set(Array.from({ length: 1000 }, () => uuid()))
    expect(seen.size).toBe(1000)
  })
})
