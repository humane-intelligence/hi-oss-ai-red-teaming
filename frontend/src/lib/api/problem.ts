// RFC 7807 Problem Details — the backend's error envelope (application/problem+json).
export type Problem = {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
  // 422 validation errors mirror Pydantic's ValidationError.errors() shape.
  errors?: { loc: (string | number)[]; msg: string; type: string }[]
}

export class ApiError extends Error {
  readonly status: number
  readonly problem: Problem

  constructor(problem: Problem, status: number) {
    super(problem.detail ?? problem.title ?? `Request failed (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
  }
}

// A refusal rather than a fault — for surfaces whose route guard is coarse, where the fetch is
// what authorizes and a 403 should read as "no access" instead of as a broken page.
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403
}

// The backend refuses every authenticated route once a version of the terms is published and the
// account has not accepted it. It shares the 403 with a permission refusal but means the opposite:
// that one is a dead end, this one is cleared by accepting — so nothing may treat them alike.
export const CONSENT_REQUIRED_TYPE = 'urn:redteam:error:terms-acceptance-required'

// Two predicates answer "is this the consent refusal", over different inputs: this one over a
// thrown `ApiError`, and `signalConsentRefusal` over a status plus a parsed `Problem`. The URN is
// shared, so it moves in one place — but a change to what *counts* as the refusal has to move both.
export function isConsentRequired(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 403 &&
    error.problem.type === CONSENT_REQUIRED_TYPE
  )
}

// Read a Problem body off a `Response`, tolerating a non-JSON one. Clones, so the caller keeps an
// unread body — for any path holding a raw `Response` rather than an `unwrap`ped result.
export async function readProblem(response: Response): Promise<Problem> {
  const parsed: unknown = await response
    .clone()
    .json()
    .catch(() => null)
  return parsed !== null && typeof parsed === 'object' ? (parsed as Problem) : {}
}

// Turn any thrown error into a message that tells the user what to do next,
// not just that something broke. Prefers the backend's own detail for most client errors.
export function humanizeError(error: unknown): string {
  if (error instanceof ApiError) {
    // Before the 403 branch: this one is not a permission problem, and saying so would send the
    // reader looking for an administrator instead of the acceptance screen.
    if (isConsentRequired(error)) return 'Accept the current terms of service to continue.'
    // 403/404 keep curated copy — their backend detail is noisy (raw ids, permission slugs).
    if (error.status === 403) return "You don't have permission to do that."
    if (error.status === 404) return 'Not found — it may have been deleted already.'
    // Surface the backend's specific detail for the remaining client errors; 5xx stays generic
    // so raw server messages never reach the user.
    if (error.status < 500 && error.problem.detail) return error.problem.detail
    if (error.status === 409) return 'That conflicts with the current state. Refresh and try again.'
    if (error.status === 429) return 'Too many requests — wait a moment and try again.'
    if (error.status >= 500) return 'Something went wrong on the server. Try again in a moment.'
    return error.message
  }
  // openapi-fetch surfaces network failures as a plain TypeError ("Failed to fetch").
  if (error instanceof TypeError)
    return "Can't reach the backend. Check that it's running and try again."
  return error instanceof Error ? error.message : 'Request failed'
}

// Flatten a 422 validation Problem into { field: message }, keyed by the last
// element of each error's `loc` (Pydantic prefixes it with "body"/"query").
export function fieldErrorsFromProblem(problem: Problem): Record<string, string> {
  const out: Record<string, string> = {}
  for (const err of problem.errors ?? []) {
    const field = err.loc.at(-1)
    if (typeof field === 'string') out[field] = err.msg
  }
  return out
}
