import { afterEach, describe, expect, it, vi } from 'vitest'
import { CONSENT_REQUIRED_EVENT, notifyConsentRefusal } from './client'
import { CONSENT_REQUIRED_TYPE } from './problem'

function refusal(type: string) {
  return new Response(JSON.stringify({ type, detail: 'nope' }), {
    status: 403,
    headers: { 'content-type': 'application/problem+json' },
  })
}

describe('notifyConsentRefusal', () => {
  // Removed in `afterEach`, not at the end of each body: a failing assertion would otherwise leak
  // the listener into the next test.
  const listener = vi.fn()

  afterEach(() => {
    window.removeEventListener(CONSENT_REQUIRED_EVENT, listener)
    listener.mockReset()
  })

  it('signals the gate for the terms refusal and leaves the body readable', async () => {
    // The caller parses its own error from the same response, so the check must not consume it.
    window.addEventListener(CONSENT_REQUIRED_EVENT, listener)
    const response = refusal(CONSENT_REQUIRED_TYPE)

    await notifyConsentRefusal(response)

    expect(listener).toHaveBeenCalledOnce()
    expect(await response.json()).toMatchObject({ detail: 'nope' })
  })

  it('stays quiet for a permission refusal and for a non-403', async () => {
    window.addEventListener(CONSENT_REQUIRED_EVENT, listener)

    await notifyConsentRefusal(refusal('about:blank'))
    await notifyConsentRefusal(new Response('nope', { status: 500 }))

    expect(listener).not.toHaveBeenCalled()
  })
})
