import type { MessageBase, MessageRole, TranscriptMessage } from '@/lib/api/types'

export type TranscriptRowKind = 'context' | 'flagged' | 'superseded'

export type TranscriptRow = {
  // `image_keys` rides only the live TranscriptMessage side — the flagged MessageBase
  // embed doesn't carry attachments, so a flagged-only (superseded) row renders none.
  message: Pick<
    MessageBase,
    'id' | 'role' | 'content' | 'slot' | 'created_at' | 'tag_context' | 'tag_context_partial'
  > & {
    role: MessageRole
    image_keys?: string[]
  }
  kind: TranscriptRowKind
}

// Live transcript excludes superseded messages, but a flag may select one that a
// later regenerate/continue replaced. Merge those back in by `created_at` so every
// flagged message stays visible and ordered; the live ones are highlighted in place.
export function mergeTranscript(
  live: TranscriptMessage[],
  flagged: MessageBase[],
  supersededIds: string[],
): TranscriptRow[] {
  const flaggedIds = new Set(flagged.map((m) => m.id))
  const supersededSet = new Set(supersededIds)
  const liveIds = new Set(live.map((m) => m.id))

  const rows: TranscriptRow[] = live.map((m) => ({
    message: m,
    kind: flaggedIds.has(m.id) ? 'flagged' : 'context',
  }))

  for (const m of flagged) {
    if (liveIds.has(m.id)) continue
    rows.push({ message: m, kind: supersededSet.has(m.id) ? 'superseded' : 'flagged' })
  }

  return rows.sort((a, b) => a.message.created_at.localeCompare(b.message.created_at))
}
