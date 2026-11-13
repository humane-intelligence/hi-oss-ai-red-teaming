import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useIsFetching } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useConversationTagsEnabled } from './queries'

const PUBLIC_URL = 'http://localhost/api/v1/platform-settings/public'

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useConversationTagsEnabled', () => {
  it('reads as off while the public settings are pending', () => {
    server.use(http.get(PUBLIC_URL, async () => delay('infinite')))
    const { result } = renderHook(() => useConversationTagsEnabled(), { wrapper })

    expect(result.current).toBe(false)
  })

  it('turns on once the settings say so', async () => {
    // The default handler answers `conversation_tags_enabled: true`; the "off" answer is pinned by
    // the page tests, since from here it is indistinguishable from the pending state.
    const { result } = renderHook(() => useConversationTagsEnabled(), { wrapper })

    await waitFor(() => expect(result.current).toBe(true))
  })

  it('fails closed when the read fails', async () => {
    let served = false
    server.use(
      http.get(PUBLIC_URL, () => {
        served = true
        return new HttpResponse(null, { status: 500 })
      }),
    )
    const { result } = renderHook(
      () => ({ enabled: useConversationTagsEnabled(), fetching: useIsFetching() }),
      { wrapper },
    )

    // Pending also reads as off, so wait for the failed read to settle before asserting.
    await waitFor(() => expect(served && result.current.fetching === 0).toBe(true))
    expect(result.current.enabled).toBe(false)
  })
})
