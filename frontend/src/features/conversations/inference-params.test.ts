import { describe, expect, it } from 'vitest'
import { buildParameters, inheritedValue, paramsToForm } from './inference-params'

describe('buildParameters', () => {
  it('coerces set numeric knobs and trims the system prompt', () => {
    expect(buildParameters({ temperature: '0.7', system_prompt: '  hi  ', top_p: '' })).toEqual({
      temperature: 0.7,
      system_prompt: 'hi',
    })
  })

  it('drops blanks and NaN, and ignores non-param form values', () => {
    expect(
      buildParameters({ temperature: '', seed: 'abc', is_disabled: true, provider: 'openai' }),
    ).toBeUndefined()
  })

  it('returns undefined when nothing is set', () => {
    expect(buildParameters({ system_prompt: '   ' })).toBeUndefined()
  })
})

describe('paramsToForm', () => {
  it('stringifies stored numbers and passes the system prompt through', () => {
    expect(paramsToForm({ temperature: 0.7, system_prompt: 'hi', stop_sequences: ['x'] })).toEqual({
      temperature: '0.7',
      system_prompt: 'hi',
    })
  })

  it('returns an empty object for null/undefined', () => {
    expect(paramsToForm(null)).toEqual({})
    expect(paramsToForm(undefined)).toEqual({})
  })
})

describe('inheritedValue', () => {
  it('stringifies the inherited value when present', () => {
    expect(inheritedValue({ temperature: 0.7 }, 'temperature')).toBe('0.7')
  })

  it('returns undefined when the key is absent, null, or empty', () => {
    // The caller renders nothing rather than a dash — an "Inherited:" line with no
    // value would claim an inheritance that does not exist.
    expect(inheritedValue(undefined, 'temperature')).toBeUndefined()
    expect(inheritedValue({ temperature: null }, 'temperature')).toBeUndefined()
    expect(inheritedValue({ temperature: '' }, 'temperature')).toBeUndefined()
  })
})
