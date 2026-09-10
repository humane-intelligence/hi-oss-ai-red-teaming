import type { FieldValues, Path, UseFormSetError } from 'react-hook-form'
import { ApiError, fieldErrorsFromProblem } from './problem'

// Map an ApiError's field-level errors[] onto react-hook-form fields. Handles a
// Pydantic 422 and a 400 that carries errors[] (e.g. a password-policy rejection)
// the same way, so the error shows inline regardless of status. `fields` narrows the
// mapping to the fields the form actually owns — without it, an errors[] entry aimed
// at a field the form does not render would count as "mapped" and vanish. Returns
// true if it mapped any (owned) field; the caller then skips its own fallback, and the
// global MutationCache likewise suppresses the toast whenever errors[] is present.
export function applyApiError<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  fields?: readonly Path<T>[],
): boolean {
  if (!(error instanceof ApiError)) return false
  const entries = Object.entries(fieldErrorsFromProblem(error.problem)).filter(
    ([name]) => !fields || fields.includes(name as Path<T>),
  )
  for (const [name, message] of entries) {
    setError(name as Path<T>, { message })
  }
  return entries.length > 0
}
