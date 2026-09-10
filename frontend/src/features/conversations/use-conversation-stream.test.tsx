import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { useConversationStream } from './use-conversation-stream'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const SSE_BODY = 'event: delta\ndata: {"content":"hi"}\n\nevent: done\ndata: {}\n\n'
const MESSAGES_URL = 'http://localhost/api/v1/evaluations/e1/conversations/c1/messages'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function mockSend() {
  const bodies: Record<string, unknown>[] = []
  server.use(
    http.post(MESSAGES_URL, async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>)
      return new HttpResponse(SSE_BODY, { headers: { 'Content-Type': 'text/event-stream' } })
    }),
  )
  return bodies
}

afterEach(() => vi.clearAllMocks())

describe('useConversationStream.send', () => {
  it('sends image_keys in attachment order alongside the content', async () => {
    const bodies = mockSend()
    const { result } = renderHook(() => useConversationStream('e1', 'c1'), { wrapper })
    await act(async () => {
      await result.current.send('describe these', ['2026/07/17/a.png', '2026/07/17/b.png'])
    })
    expect(bodies[0]?.content).toBe('describe these')
    expect(bodies[0]?.image_keys).toEqual(['2026/07/17/a.png', '2026/07/17/b.png'])
    expect(typeof bodies[0]?.client_message_id).toBe('string')
  })

  it('omits image_keys entirely for a text-only message', async () => {
    const bodies = mockSend()
    const { result } = renderHook(() => useConversationStream('e1', 'c1'), { wrapper })
    await act(async () => {
      await result.current.send('just text')
    })
    expect(bodies[0]).not.toHaveProperty('image_keys')
  })

  it('returns false and toasts when the fetch throws (dropped connection)', async () => {
    // A network error rejects the fetch before any event — the error-event path never runs.
    server.use(http.post(MESSAGES_URL, () => HttpResponse.error()))
    const { result } = renderHook(() => useConversationStream('e1', 'c1'), { wrapper })
    let ok: boolean | undefined
    await act(async () => {
      ok = await result.current.send('hello')
    })
    expect(ok).toBe(false)
    expect(toast.error).toHaveBeenCalledWith('Connection lost. Please try again.')
  })
})
