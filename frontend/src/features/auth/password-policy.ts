import { z } from 'zod'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

// Mirrors of the backend's rules, deliberately no stricter than Python's — not equivalent to it.
// `\p{Uppercase}` is the binary property `str.isupper` uses (`\p{Lu}` alone would reject `Ⅷ`); `\p{N}`
// is a strict *superset* of `str.isdigit` (`½` and `Ⅷ` satisfy it and would not satisfy the backend)
// and `[^\p{L}\p{N}]` a superset of `not str.isalnum()`. Looser is the safe direction: the server
// refuses what this lets through, never the reverse. It only spares the user a round-trip to learn
// a published rule — the backend stays the authority.
//
// Length is the one rule where the direction depends on which end you are on, because JS counts
// UTF-16 units and Python code points ('🔒🔒🔒🔒'.length === 8 against Python's 4). That makes a
// minimum looser (safe) and a maximum *stricter* — 65 astral characters are 130 units here and 65
// code points to `max_length=128`, so a plain `.max()` would refuse what the API accepts. Hence the
// cap counts code points explicitly while the minimum stays on the units it gets for free.
const UPPERCASE = /\p{Uppercase}/u
const DIGIT = /\p{N}/u
const SYMBOL = /[^\p{L}\p{N}]/u

// The cap every install enforces (the set-password schemas carry it into the contract); unlike
// the minimum it is not admin-tunable, so it lives here rather than in the published policy.
const MAX_LENGTH = 128

// Shared so the password inputs can point `aria-describedby` at the rendered list.
export const PASSWORD_REQUIREMENTS_ID = 'password-requirements'

export function passwordSchema(policy: PublicPasswordPolicyResponse) {
  return z
    .string()
    .min(policy.min_length, `At least ${policy.min_length} characters`)
    .refine((value) => [...value].length <= MAX_LENGTH, {
      message: `At most ${MAX_LENGTH} characters`,
    })
    .refine((value) => !policy.require_uppercase || UPPERCASE.test(value), {
      message: 'Add an uppercase letter',
    })
    .refine((value) => !policy.require_digit || DIGIT.test(value), { message: 'Add a digit' })
    .refine((value) => !policy.require_symbol || SYMBOL.test(value), { message: 'Add a symbol' })
}

// Enforced on every install, so not part of the published policy — but listed all the same: they
// are the two rules a locally-valid password can still trip, and being told by a 400 is exactly
// what this list exists to replace. Neither can be checked here (one needs the breach denylist,
// the other the account identity), so they carry no local validation.
const ALWAYS_ENFORCED = ['Not a common or breached password', 'Not similar to your email or name']

export function passwordRequirements(policy: PublicPasswordPolicyResponse): string[] {
  return [
    `At least ${policy.min_length} characters`,
    ...(policy.require_uppercase ? ['An uppercase letter'] : []),
    ...(policy.require_digit ? ['A digit'] : []),
    ...(policy.require_symbol ? ['A symbol'] : []),
    ...ALWAYS_ENFORCED,
  ]
}
