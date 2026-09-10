import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { streamSse } from '@/lib/api/stream'
import { uuid } from '@/lib/uuid'
import type { UserMessageIn } from '@/lib/api/types'

type Streaming = { userText?: string; assistantText: string; imageKeys?: string[] }

export function useConversationStream(evaluationId: string, conversationId: string) {
  const qc = useQueryClient()
  const [streaming, setStreaming] = useState<Streaming | null>(null)
  const [pending, setPending] = useState(false)

  const base = `/api/v1/evaluations/${evaluationId}/conversations/${conversationId}`

  const run = async (path: string, body: unknown, userText?: string, imageKeys?: string[]) => {
    setPending(true)
    setStreaming({ userText, assistantText: '', imageKeys })
    let text = ''
    let lastCommit = 0
    let ok = true

    try {
      await streamSse(path, body, (event) => {
        if (event.type === 'delta') {
          text += event.content
          // Reveal as it arrives, but throttle re-renders to ~one per frame so a
          // fast token stream doesn't thrash React (and the markdown re-parse).
          const now = performance.now()
          if (now - lastCommit >= 50) {
            lastCommit = now
            setStreaming((s) => (s ? { ...s, assistantText: text } : s))
          }
        } else if (event.type === 'error') {
          ok = false
          // A retryable failure is transient (rate limit / timeout / a self-hosted
          // endpoint still waking) — nudge the user to retry rather than treating it as fatal.
          toast.error(
            event.detail,
            event.retryable
              ? { description: 'This looks temporary — try again in a moment.' }
              : undefined,
          )
        }
      })
      // Show the finished reply, then hand off to the persisted transcript.
      setStreaming((s) => (s ? { ...s, assistantText: text } : s))
    } catch {
      // A thrown fetch (offline / dropped connection) never emits an error event — surface it
      // here so the composer doesn't stay stuck pending with the message silently lost.
      ok = false
      toast.error('Connection lost. Please try again.')
    } finally {
      setPending(false)
      setStreaming(null)
      qc.invalidateQueries({ queryKey: ['messages', conversationId] })
      qc.invalidateQueries({ queryKey: ['conversation', conversationId] })
    }
    return ok
  }

  return {
    streaming,
    pending,
    send: (content: string, imageKeys?: string[], tags?: UserMessageIn['tags']) =>
      run(
        `${base}/messages`,
        {
          content,
          client_message_id: uuid(),
          ...(imageKeys?.length ? { image_keys: imageKeys } : {}),
          ...(tags && Object.keys(tags).length ? { tags } : {}),
        },
        content,
        imageKeys?.length ? imageKeys : undefined,
      ),
    regenerate: (messageId: string) => run(`${base}/messages/${messageId}/regenerate`, undefined),
    continueReply: (messageId: string) => run(`${base}/messages/${messageId}/continue`, undefined),
  }
}
