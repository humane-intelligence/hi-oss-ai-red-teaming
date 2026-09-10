import { PASSWORD_REQUIREMENTS_ID, passwordRequirements } from './password-policy'
import type { PublicPasswordPolicyResponse } from '@/lib/api/types'

// Stated up front rather than after a rejection — these screens are anonymous, so the rules
// arrive with the form or not at all. While the published policy is still loading the list says
// so instead of showing the shipped fallback, which would flip to the real rules mid-read; the
// element stays mounted either way so the password field's `aria-describedby` keeps its target.
// `pending` is required: a caller that omitted it would silently reintroduce that flip.
export function PasswordRequirements({
  policy,
  pending,
}: {
  policy: PublicPasswordPolicyResponse
  pending: boolean
}) {
  return (
    <ul
      id={PASSWORD_REQUIREMENTS_ID}
      className="text-muted-foreground list-inside list-disc text-xs"
      aria-label="Password requirements"
      aria-live="polite"
    >
      {pending ? (
        <li>Checking the password rules — the server enforces them regardless.</li>
      ) : (
        passwordRequirements(policy).map((requirement) => <li key={requirement}>{requirement}</li>)
      )}
    </ul>
  )
}
