import type { NoteResponse } from '@/lib/api/types'

export type NoteIndex = {
  /** Anchor message id -> the notes whose body renders there, oldest first. */
  byAnchor: Map<string, NoteResponse[]>
  /**
   * Covered rows that are not the anchor of the note covering them — they carry the
   * "another note also covers this row" marker. A row can be one note's anchor *and* be
   * covered by a second note, and it needs the marker either way; a row-local
   * `notes.length === 0` test loses it exactly then.
   */
  crossReferenced: Set<string>
  /** Notes whose whole selection is absent from this transcript. */
  unanchored: NoteResponse[]
}

/**
 * Map notes onto transcript rows.
 *
 * A note covers a set of messages, so the relation is many-to-many. The body renders
 * once — on the earliest covered row in transcript order — because repeating a
 * 10 000-character note under every message it covers would swamp the transcript.
 * The anchor comes from `orderedMessageIds` (this transcript's own order) rather than
 * from `message_ids`: the note's selection can include messages this view does not
 * show at all, and a selection is not required to be contiguous.
 */
export function indexNotes(items: NoteResponse[], orderedMessageIds: string[]): NoteIndex {
  const position = new Map(orderedMessageIds.map((id, i) => [id, i]))
  const byAnchor = new Map<string, NoteResponse[]>()
  const crossReferenced = new Set<string>()
  const unanchored: NoteResponse[] = []

  for (const note of items) {
    let anchor: string | undefined
    let anchorAt = Number.POSITIVE_INFINITY
    const visible: string[] = []
    for (const messageId of note.message_ids) {
      const at = position.get(messageId)
      if (at === undefined) continue
      visible.push(messageId)
      if (at < anchorAt) {
        anchorAt = at
        anchor = messageId
      }
    }
    if (anchor === undefined) {
      unanchored.push(note)
      continue
    }
    for (const messageId of visible) if (messageId !== anchor) crossReferenced.add(messageId)
    const bucket = byAnchor.get(anchor)
    if (bucket) bucket.push(note)
    else byAnchor.set(anchor, [note])
  }

  for (const bucket of byAnchor.values()) {
    bucket.sort((a, b) => a.created_at.localeCompare(b.created_at))
  }
  return { byAnchor, crossReferenced, unanchored }
}
