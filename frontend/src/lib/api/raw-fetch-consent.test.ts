import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/msw/server'
import { CONSENT_REQUIRED_EVENT } from './client'
import { CONSENT_REQUIRED_TYPE, isConsentRequired } from './problem'
import { downloadFile } from './download'
import { fetchImageBlob } from './images'
import { streamSse } from './stream'

// The three paths that bypass the typed client's middleware and so have to signal the gate
// themselves. Each case fails if its own `signalConsentRefusal` call is removed.
const listener = vi.fn()

function refuse() {
  return HttpResponse.json(
    { type: CONSENT_REQUIRED_TYPE, detail: 'Accept the current terms of service to continue.' },
    { status: 403, headers: { 'content-type': 'application/problem+json' } },
  )
}

afterEach(() => {
  window.removeEventListener(CONSENT_REQUIRED_EVENT, listener)
  listener.mockReset()
})

describe('raw fetch paths signal the consent gate', () => {
  it('downloadFile fires the gate and throws a refusal the caller can recognise', async () => {
    window.addEventListener(CONSENT_REQUIRED_EVENT, listener)
    server.use(http.get('http://localhost/api/v1/exports/jobs/j1/download', () => refuse()))

    await expect(downloadFile('/api/v1/exports/jobs/j1/download', 'x.csv')).rejects.toSatisfy(
      isConsentRequired,
    )

    expect(listener).toHaveBeenCalledOnce()
  })

  it('fetchImageBlob fires the gate when the signed fetch is refused', async () => {
    // The mint succeeds, so only the raw fetch below can be what signals the gate.
    window.addEventListener(CONSENT_REQUIRED_EVENT, listener)
    server.use(
      http.get('http://localhost/api/v1/images/signed-url', () =>
        HttpResponse.json({ url: '/api/v1/images/signed/tok', expires_at: '2026-07-17T01:00:00Z' }),
      ),
      http.get('http://localhost/api/v1/images/signed/tok', () => refuse()),
    )

    await expect(fetchImageBlob('2026/07/17/a.png')).rejects.toSatisfy(isConsentRequired)

    expect(listener).toHaveBeenCalledOnce()
  })

  it('streamSse fires the gate and reports the refusal inline', async () => {
    window.addEventListener(CONSENT_REQUIRED_EVENT, listener)
    server.use(http.post('http://localhost/api/v1/conversations/c1/messages', () => refuse()))
    const events: unknown[] = []

    await streamSse('/api/v1/conversations/c1/messages', { content: 'hi' }, (e) => events.push(e))

    expect(listener).toHaveBeenCalledOnce()
    expect(events).toEqual([
      { type: 'error', detail: 'Accept the current terms of service to continue.' },
    ])
  })
})
