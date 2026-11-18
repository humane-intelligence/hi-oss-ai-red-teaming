import type { EvaluationStatus, PublicationStatus } from '@/lib/api/types'

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

// Mirrors the backend gate on starting and continuing conversations: a published group takes
// prompts, an unpublished one only from whoever may edit the evaluation (to try a scenario out),
// a finished group and a rejected or completed evaluation from nobody. `undefined` while either
// status is still unknown — a reason is only given for what is known to block.
export function promptsBlockedReason(
  groupStatus: PublicationStatus | undefined,
  evaluationStatus: EvaluationStatus | undefined,
  mayEditEvaluation: boolean,
): string | undefined {
  if (groupStatus === undefined || evaluationStatus === undefined) return undefined
  if (groupStatus === 'inactive')
    return 'This evaluation group is finished, so its conversations are read-only.'
  if (evaluationStatus === 'rejected' || evaluationStatus === 'completed')
    return 'This evaluation is closed, so its conversations are read-only.'
  if (groupStatus === 'published' || mayEditEvaluation) return undefined
  return 'This evaluation group is not published yet; conversations can start once it is.'
}
