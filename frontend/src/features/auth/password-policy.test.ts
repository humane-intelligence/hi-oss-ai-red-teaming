import { describe, expect, it } from 'vitest'
import { passwordRequirements, passwordSchema } from './password-policy'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

function policy(
  overrides: Partial<PublicPasswordPolicyResponse> = {},
): PublicPasswordPolicyResponse {
  return {
    min_length: 8,
    require_uppercase: false,
    require_digit: false,
    require_symbol: false,
    ...overrides,
  }
}

const messageFor = (schema: ReturnType<typeof passwordSchema>, value: string) => {
  const result = schema.safeParse(value)
  return result.success ? null : result.error.issues[0]?.message
}

describe('passwordSchema', () => {
  it('accepts a plain passphrase under the shipped policy', () => {
    expect(messageFor(passwordSchema(policy()), 'correcthorse')).toBeNull()
  })

  it('refuses a password past the contract cap the policy cannot raise', () => {
    expect(messageFor(passwordSchema(policy()), 'x'.repeat(129))).toBe('At most 128 characters')
  })

  it('accepts a password of exactly the contract cap', () => {
    expect(messageFor(passwordSchema(policy()), 'x'.repeat(128))).toBeNull()
  })

  it('counts the cap in code points, so astral characters are not double-charged', () => {
    // `max_length=128` on the API counts code points; JS string length counts UTF-16 units, so a
    // `.max()` here would refuse this at 130 units while the backend accepts it at 65.
    const sixtyFiveEmoji = '\u{1F512}'.repeat(65)
    expect(sixtyFiveEmoji.length).toBe(130)
    expect(messageFor(passwordSchema(policy()), sixtyFiveEmoji)).toBeNull()
  })

  it('reports the configured minimum, not a fixed one', () => {
    expect(messageFor(passwordSchema(policy({ min_length: 16 })), 'twelvechars1')).toBe(
      'At least 16 characters',
    )
  })

  it.each([
    ['require_uppercase', 'lowercase123', 'Add an uppercase letter'],
    ['require_digit', 'NoDigitsHere', 'Add a digit'],
    ['require_symbol', 'NoSymbols1234', 'Add a symbol'],
  ] as const)('reports a missing %s', (knob, value, message) => {
    expect(messageFor(passwordSchema(policy({ [knob]: true })), value)).toBe(message)
  })

  it('counts a non-ASCII symbol, matching the backend rule', () => {
    // The backend's rule is "neither a letter nor a number" — a stricter mirror here would
    // reject a password the API accepts.
    expect(messageFor(passwordSchema(policy({ require_symbol: true })), 'passphrase§1')).toBeNull()
  })
})

describe('passwordRequirements', () => {
  const ALWAYS = ['Not a common or breached password', 'Not similar to your email or name']

  it('lists only the configurable rules in force', () => {
    expect(passwordRequirements(policy())).toEqual(['At least 8 characters', ...ALWAYS])
    expect(passwordRequirements(policy({ min_length: 12, require_digit: true }))).toEqual([
      'At least 12 characters',
      'A digit',
      ...ALWAYS,
    ])
  })

  it('always lists the two rules the backend enforces unconditionally', () => {
    // They are not in the published policy, so nothing here would surface them — and they are the
    // two a locally-valid password can still trip, which is what the list exists to prevent.
    expect(passwordRequirements(policy({ require_symbol: true })).slice(-2)).toEqual(ALWAYS)
  })
})
