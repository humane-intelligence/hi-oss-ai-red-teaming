import type { ReviewResponse } from '@/lib/api/types'

// Only the traits that hold, joined — far more scannable than "yes · no · yes".
// Empty (a rejected/clean verdict with no positive markers) reads as "—".
export function verdictSummary(r: ReviewResponse): string {
  const parts: string[] = []
  if (r.successful_exploit) parts.push('successful')
  if (r.unique_exploit) parts.push('unique')
  if (r.valid_submission) parts.push('valid')
  if (r.number_prompts != null) parts.push(`${r.number_prompts} prompts`)
  return parts.length ? parts.join(' · ') : '—'
}
