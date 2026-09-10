import { tokenStore } from '@/lib/auth/token-store'
import { signalConsentRefusal, UNAUTHORIZED_EVENT } from '@/lib/api/client'
import { ApiError, readProblem } from '@/lib/api/problem'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

/**
 * Download an authed file response (e.g. a finished CSV export) as a browser download.
 *
 * openapi-fetch is request/response-JSON only, so file downloads use a raw fetch
 * with the same bearer header the typed client injects (mirrors `streamSse`). The
 * filename comes from `Content-Disposition`, falling back to `fallbackName`. A 401 clears the
 * token and a terms refusal signals the acceptance gate, both like the openapi-fetch middleware.
 */
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const token = tokenStore.get()
  const response = await fetch(`${BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    if (response.status === 401 && token) {
      tokenStore.clear()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    // Parsed once: the refusal signal and the thrown error both come from this body. An
    // `ApiError`, not a plain one, so downstream can recognise the consent refusal and leave the
    // reporting to the gate.
    const problem = await readProblem(response)
    signalConsentRefusal(response.status, problem)
    throw new ApiError(problem, response.status)
  }
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const name = /filename="?([^"]+)"?/.exec(disposition)?.[1] ?? fallbackName
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Defer the revoke a tick: some engines (WebKit) cancel the download if the blob URL is freed
  // in the same synchronous frame as click().
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
