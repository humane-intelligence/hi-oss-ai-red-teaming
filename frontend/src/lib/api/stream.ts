import { signalConsentRefusal, UNAUTHORIZED_EVENT } from '@/lib/api/client'
import { readProblem } from '@/lib/api/problem'
import { tokenStore } from '@/lib/auth/token-store'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

export type StreamEvent =
  | { type: 'delta'; content: string }
  | { type: 'done'; finish_reason?: string }
  | { type: 'error'; detail: string; retryable?: boolean }

// Consume a Server-Sent Events stream from a POST endpoint. openapi-fetch is
// request/response-only, so the streaming chat endpoints are driven here with raw
// fetch + a ReadableStream reader. The bearer header, the global 401 handling and the terms
// refusal all mirror the openapi-fetch middleware.
export async function streamSse(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = tokenStore.get()
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  // Resolution/auth failures arrive as application/problem+json before any event.
  if (!res.ok) {
    if (res.status === 401 && token) {
      tokenStore.clear()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    // A version published mid-conversation refuses this too; without it the reader gets an inline
    // error and the gate only on the next reload.
    const problem = await readProblem(res)
    signalConsentRefusal(res.status, problem)
    onEvent({
      type: 'error',
      detail: problem.detail ?? problem.title ?? `Request failed (${res.status})`,
    })
    return
  }

  const reader = res.body?.getReader()
  if (!reader) return
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const event = parseFrame(frame)
      if (event) onEvent(event)
    }
    // Yield a macrotask between reads so the browser can paint and React's
    // scheduler can run; the read-loop otherwise chains microtasks back-to-back
    // and starves rendering until the whole stream has been consumed.
    await new Promise((resolve) => setTimeout(resolve))
  }
}

function parseFrame(frame: string): StreamEvent | null {
  let name = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null

  const data = safeJsonObject(dataLines.join('\n'))

  if (name === 'delta') {
    return { type: 'delta', content: typeof data.content === 'string' ? data.content : '' }
  }
  if (name === 'done') {
    return {
      type: 'done',
      finish_reason: typeof data.finish_reason === 'string' ? data.finish_reason : undefined,
    }
  }
  if (name === 'error') {
    const detail =
      typeof data.detail === 'string'
        ? data.detail
        : typeof data.title === 'string'
          ? data.title
          : 'Stream error'
    return {
      type: 'error',
      detail,
      retryable: typeof data.retryable === 'boolean' ? data.retryable : undefined,
    }
  }
  return null
}

function safeJsonObject(text: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(text)
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}
