import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

describe('copyText without the clipboard API', () => {
  afterEach(() => {
    Reflect.deleteProperty(document, 'execCommand')
  })

  it('rejects when the legacy copy reports failure, leaving no textarea behind', async () => {
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn(() => false),
      configurable: true,
    })

    await expect(copyText('abc')).rejects.toThrow('copy failed')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('removes the textarea when the legacy copy throws', async () => {
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn(() => {
        throw new Error('blocked')
      }),
      configurable: true,
    })

    await expect(copyText('abc')).rejects.toThrow('blocked')
    expect(document.querySelector('textarea')).toBeNull()
  })
})
