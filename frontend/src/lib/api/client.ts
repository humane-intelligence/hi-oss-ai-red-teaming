import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import { tokenStore } from '@/lib/auth/token-store'
import { CONSENT_REQUIRED_TYPE, readProblem, type Problem } from './problem'

// Fired when a request 401s while we held a token (expired/revoked session).
export const UNAUTHORIZED_EVENT = 'auth:unauthorized'

// Fired when a request is refused because the account owes a terms acceptance. The backend
// enforces that on every authenticated route, so this is what turns a refusal into the gate
// without waiting for a reload — the held identity is all the gate reads.
export const CONSENT_REQUIRED_EVENT = 'auth:consent-required'

// Every path that reaches the backend has to recognise the refusal, not just the typed client:
// the SSE stream, file downloads and image fetches all run on a raw `fetch`, and a conversation is
// where a reader is most likely to be sitting when a version lands.
export function signalConsentRefusal(status: number, problem: Problem): void {
  if (status === 403 && problem.type === CONSENT_REQUIRED_TYPE) {
    window.dispatchEvent(new Event(CONSENT_REQUIRED_EVENT))
  }
}

// For the typed client's middleware, which holds a `Response` it has not parsed. A path that parses
// the body anyway (the raw fetches, for their `ApiError`) calls `signalConsentRefusal` with what it
// already has rather than reading the body a second time.
export async function notifyConsentRefusal(response: Response): Promise<void> {
  if (response.status !== 403) return
  signalConsentRefusal(response.status, await readProblem(response))
}

// The OpenAPI spec declares no security scheme, so the bearer header is injected
// here rather than auto-wired by the generated client.
const authMiddleware: Middleware = {
  onRequest({ request }) {
    const token = tokenStore.get()
    if (token) request.headers.set('Authorization', `Bearer ${token}`)
    return request
  },
  async onResponse({ response }) {
    if (response.status === 401 && tokenStore.get()) {
      tokenStore.clear()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    await notifyConsentRefusal(response)
    return response
  },
}

// Single typed entry point to the backend.
export const apiClient = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL || '',
})

apiClient.use(authMiddleware)
