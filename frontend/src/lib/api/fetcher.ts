import { ApiError, type Problem } from './problem'

// Normalizes an openapi-fetch result into "return data or throw ApiError",
// so query/mutation functions can stay one-liners. Error UX is layered on later.
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok || result.error !== undefined) {
    throw new ApiError((result.error ?? {}) as Problem, result.response.status)
  }
  return result.data as T
}
