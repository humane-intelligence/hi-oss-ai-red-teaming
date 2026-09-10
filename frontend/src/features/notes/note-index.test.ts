import { describe, expect, it } from 'vitest'
import { indexNotes } from './note-index'
import type { NoteResponse } from '@/lib/api/types'

const MSG_1 = '11111111-1111-4111-8111-111111111111'
const MSG_2 = '22222222-2222-4222-8222-222222222222'
const MSG_3 = '33333333-3333-4333-8333-333333333333'
const OFF_TRANSCRIPT = '99999999-9999-4999-8999-999999999999'
const ORDER = [MSG_1, MSG_2, MSG_3]

// Typed, not cast: the fixture is what makes `make gen` surface a contract change here.
function note(
  id: string,
  messageIds: string[],
  text: string,
  createdAt = '2026-01-01T12:00:00Z',
): NoteResponse {
  return {
    id,
    conversation_id: 'conv-1',
    message_ids: messageIds,
    text,
    created_by_id: 'user-1',
    evaluation_id: 'eval-1',
    evaluation_group_id: 'group-1',
    created_at: createdAt,
    updated_at: createdAt,
  }
}

describe('indexNotes', () => {
  it('anchors a single-message note on that message and nowhere else', () => {
    const index = indexNotes([note('a', [MSG_2], 'on the second')], ORDER)

    expect(index.byAnchor.get(MSG_2)?.map((n) => n.text)).toEqual(['on the second'])
    expect(index.byAnchor.get(MSG_1)).toBeUndefined()
    expect(index.byAnchor.get(MSG_3)).toBeUndefined()
  })

  it('renders a multi-message note once, on its earliest covered message', () => {
    const index = indexNotes([note('a', [MSG_2, MSG_3], 'spans two')], ORDER)

    expect(index.byAnchor.get(MSG_2)?.map((n) => n.text)).toEqual(['spans two'])
    expect(index.byAnchor.get(MSG_3)).toBeUndefined()
  })

  it('takes the anchor from transcript order, not from the payload order', () => {
    // The API does not promise the embedded selection is transcript-ordered.
    const index = indexNotes([note('a', [MSG_3, MSG_1], 'reversed payload')], ORDER)

    expect(index.byAnchor.get(MSG_1)?.map((n) => n.text)).toEqual(['reversed payload'])
    expect(index.byAnchor.get(MSG_3)).toBeUndefined()
  })

  it('collects a note whose whole selection is off-transcript instead of dropping it', () => {
    const index = indexNotes([note('a', [OFF_TRANSCRIPT], 'nowhere to anchor')], ORDER)

    expect(index.unanchored.map((n) => n.text)).toEqual(['nowhere to anchor'])
    expect(index.byAnchor.size).toBe(0)
  })

  it('anchors a partially visible note on the visible message rather than orphaning it', () => {
    const index = indexNotes([note('a', [OFF_TRANSCRIPT, MSG_3], 'partly visible')], ORDER)

    expect(index.byAnchor.get(MSG_3)?.map((n) => n.text)).toEqual(['partly visible'])
    expect(index.unanchored).toEqual([])
  })

  it('keeps the cross-reference marker on a row that also anchors its own note', () => {
    // The row-local `notes.length === 0` test lost the marker exactly here: MSG_2 anchors note 'b'
    // and is also covered by note 'a', so it needs both its body and the "also covered" marker.
    const index = indexNotes(
      [note('a', [MSG_1, MSG_2], 'spans one and two'), note('b', [MSG_2], 'on the second')],
      ORDER,
    )

    expect(index.byAnchor.get(MSG_1)?.map((n) => n.text)).toEqual(['spans one and two'])
    expect(index.byAnchor.get(MSG_2)?.map((n) => n.text)).toEqual(['on the second'])
    expect(index.crossReferenced.has(MSG_2)).toBe(true)
    // MSG_1 is note 'a''s anchor, so it renders the body and needs no marker.
    expect(index.crossReferenced.has(MSG_1)).toBe(false)
  })

  it('orders several notes on one message oldest-first', () => {
    // The server returns -created_at; a row should read in the order it was written.
    const index = indexNotes(
      [
        note('b', [MSG_1], 'newer', '2026-02-01T12:00:00Z'),
        note('a', [MSG_1], 'older', '2026-01-01T12:00:00Z'),
      ],
      ORDER,
    )

    expect(index.byAnchor.get(MSG_1)?.map((n) => n.text)).toEqual(['older', 'newer'])
  })
})
