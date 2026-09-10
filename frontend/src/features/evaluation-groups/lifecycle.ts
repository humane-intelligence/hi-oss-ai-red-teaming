import type { PublicationStatus } from '@/lib/api/types'

// Mirrors the backend allowlist (`GROUP_STATUSES_ACCEPTING_EVALUATIONS`): evaluations
// may be added only to an approved or published group — every other lifecycle state
// 409s, with no admin bypass — so the UI hides add/duplicate affordances the API
// would reject. A non-accepting group can still *contain* evaluations (group
// duplication deep-copies them into a draft); only adding is gated.
const ACCEPTING_STATUSES: readonly PublicationStatus[] = ['approved', 'published']

export function groupAcceptsEvaluations(status: PublicationStatus | undefined): boolean {
  return status !== undefined && ACCEPTING_STATUSES.includes(status)
}

// Why adding is blocked, phrased for the state: pre-approval groups can still get
// there ("once approved"); rejected and finished are terminal — promising a future
// approval would mislead.
export function evaluationsBlockedHint(status: PublicationStatus | undefined): string {
  if (status === 'not_approved')
    return 'This group was not approved — evaluations cannot be added to it.'
  if (status === 'inactive') return 'This group is finished — evaluations can no longer be added.'
  return 'Evaluations can be added once the group is approved.'
}
