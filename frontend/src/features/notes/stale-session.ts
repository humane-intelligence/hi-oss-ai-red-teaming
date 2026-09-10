import { ApiError, humanizeError } from '@/lib/api/problem'

// A 403 on a note call almost always means the caller's token predates the
// permission grant: the UI gates on /auth/me (resolved live from the DB) while the
// backend gates on the JWT claim, frozen at mint for up to the session TTL. The
// generic "you don't have access" copy would send the user to an admin; this sends
// them to the one action that fixes it.
export const STALE_SESSION_HINT =
  'Your session predates this permission. Sign out and back in to refresh it.'

// Reading every 403 as a stale token is only sound because the note routes answer a
// visibility failure with 404, never 403 — an unreachable conversation or another author's note
// reads as missing by design (no existence leak). If that ever changes, this copy starts lying.
export function noteErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) return STALE_SESSION_HINT
  return humanizeError(error)
}
