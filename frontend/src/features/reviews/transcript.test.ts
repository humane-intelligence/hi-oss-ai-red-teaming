import { describe, expect, it } from 'vitest'
import { mergeTranscript } from './transcript'
import type { MessageBase, TranscriptMessage } from '@/lib/api/types'

function live(id: string, created_at: string, content = id): TranscriptMessage {
  return {
    id,
    role: 'assistant',
    status: 'complete',
    content,
    created_at,
    turn_id: 't',
    tag_context_partial: false,
  }
}

function flagged(id: string, created_at: string, content = id): MessageBase {
  return {
    id,
    role: 'assistant',
    status: 'complete',
    content,
    created_at,
    tag_context_partial: false,
  }
}

describe('mergeTranscript', () => {
  it('marks live messages flagged or context, preserving order', () => {
    const rows = mergeTranscript(
      [live('a', '2026-01-01T00:00:00Z'), live('b', '2026-01-01T00:01:00Z')],
      [flagged('b', '2026-01-01T00:01:00Z')],
      [],
    )
    expect(rows.map((r) => [r.message.id, r.kind])).toEqual([
      ['a', 'context'],
      ['b', 'flagged'],
    ])
  })

  it('inserts a superseded flagged message at its chronological slot', () => {
    const rows = mergeTranscript(
      [live('a', '2026-01-01T00:00:00Z'), live('c', '2026-01-01T00:02:00Z')],
      [flagged('b', '2026-01-01T00:01:00Z')],
      ['b'],
    )
    expect(rows.map((r) => [r.message.id, r.kind])).toEqual([
      ['a', 'context'],
      ['b', 'superseded'],
      ['c', 'context'],
    ])
  })

  it('carries image_keys through on live rows (thumbnails in the review transcript)', () => {
    const withImages = { ...live('a', '2026-01-01T00:00:00Z'), image_keys: ['k/one.png'] }
    const rows = mergeTranscript([withImages], [], [])
    expect(rows[0]!.message.image_keys).toEqual(['k/one.png'])
  })

  it('does not duplicate a flagged message that is live', () => {
    const rows = mergeTranscript(
      [live('a', '2026-01-01T00:00:00Z')],
      [flagged('a', '2026-01-01T00:00:00Z')],
      [],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]!.kind).toBe('flagged')
  })

  it('renders a flagged message absent from the live transcript and not superseded as flagged', () => {
    const rows = mergeTranscript(
      [live('a', '2026-01-01T00:00:00Z')],
      [flagged('x', '2026-01-01T00:00:30Z')],
      [],
    )
    expect(rows.map((r) => [r.message.id, r.kind])).toEqual([
      ['a', 'context'],
      ['x', 'flagged'],
    ])
  })

  // Pins the runtime copy-through; it does not guard the `Pick<MessageBase, ...>` widening that
  // exposes `tag_context` on `TranscriptRow['message']` — `mergeTranscript` passes whole objects
  // through regardless of that type, so only `tsc` (`make lint`) would catch its removal.
  it('keeps the recorded tag context from the live and the flagged half', () => {
    const liveRow = { ...live('a', '2026-01-01T00:00:00Z'), tag_context: { env: 'prod' } }
    const flaggedRow = { ...flagged('b', '2026-01-01T00:01:00Z'), tag_context: { env: 'dev' } }

    const rows = mergeTranscript([liveRow], [flaggedRow], [])

    expect(rows.map((r) => r.message.tag_context)).toEqual([{ env: 'prod' }, { env: 'dev' }])
  })
})
