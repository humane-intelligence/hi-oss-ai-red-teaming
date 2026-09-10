import { describe, expect, it } from 'vitest'
import { annotationLabel, annotationValue, indexAnnotations } from './annotation-index'
import type { AnnotationResponse } from '@/lib/api/types'

const MSG_1 = 'msg-0001'
const MSG_2 = 'msg-0002'
const OFF_TRANSCRIPT = 'msg-gone'
const ORDER = [MSG_1, MSG_2]

function annotation(
  id: string,
  messageId: string,
  label: { id: string; key: string | null; name: string; is_custom: boolean },
): AnnotationResponse {
  return {
    id,
    message_id: messageId,
    conversation_id: 'conv-1',
    label,
    created_by_id: 'user-1',
    evaluation_id: 'eval-1',
    evaluation_group_id: 'group-1',
    created_at: '2026-08-27T12:00:00Z',
    updated_at: '2026-08-27T12:00:00Z',
  } as AnnotationResponse
}

const JAILBREAK = { id: 'label-jb', key: 'jailbreak', name: 'Jailbreak', is_custom: false }
const BIAS = { id: 'label-bias', key: 'bias', name: 'Bias', is_custom: false }
const custom = (id: string, name: string) => ({ id, key: null, name, is_custom: true })

describe('annotationLabel / annotationValue', () => {
  it('reads a catalog label from the embedded entry', () => {
    const a = annotation('a', MSG_1, JAILBREAK)

    expect(annotationLabel(a)).toBe('Jailbreak')
    expect(annotationValue(a)).toBe('label-jb')
  })

  it('reads a custom label the same way — every annotation references a label row', () => {
    // A typed label is a label row too (scoped to its author), so there is no second
    // namespace: the picker value is always the label's id.
    const a = annotation('a', MSG_1, custom('label-pi', 'prompt injection'))

    expect(annotationLabel(a)).toBe('prompt injection')
    expect(annotationValue(a)).toBe('label-pi')
  })
})

describe('indexAnnotations', () => {
  it('buckets annotations by their single anchor message', () => {
    const index = indexAnnotations(
      [annotation('a', MSG_1, JAILBREAK), annotation('b', MSG_2, BIAS)],
      ORDER,
    )

    expect(index.byMessage.get(MSG_1)?.map((a) => a.id)).toEqual(['a'])
    expect(index.byMessage.get(MSG_2)?.map((a) => a.id)).toEqual(['b'])
    expect(index.unanchored).toEqual([])
  })

  it('orders curated labels before custom ones, each alphabetically', () => {
    // The curated vocabulary is the shared language, so it reads first; a stable order stops
    // chips reshuffling as other annotators add their own.
    const index = indexAnnotations(
      [
        annotation('adhoc-z', MSG_1, custom('l-z', 'zebra')),
        annotation('cat-jb', MSG_1, JAILBREAK),
        annotation('adhoc-a', MSG_1, custom('l-a', 'alpha')),
        annotation('cat-bias', MSG_1, BIAS),
      ],
      ORDER,
    )

    expect(index.byMessage.get(MSG_1)?.map((a) => a.id)).toEqual([
      'cat-bias',
      'cat-jb',
      'adhoc-a',
      'adhoc-z',
    ])
  })

  it('keeps an annotation whose message is absent instead of dropping it', () => {
    // The server keeps listing annotations on superseded messages, which the transcript stops
    // rendering — a count that lost them would understate the labelling.
    const index = indexAnnotations(
      [annotation('a', OFF_TRANSCRIPT, JAILBREAK), annotation('b', MSG_1, BIAS)],
      ORDER,
    )

    expect(index.unanchored.map((a) => a.id)).toEqual(['a'])
    expect(index.byMessage.get(OFF_TRANSCRIPT)).toBeUndefined()
    expect(index.byMessage.get(MSG_1)?.map((a) => a.id)).toEqual(['b'])
  })

  it('returns empty structures for no annotations', () => {
    const index = indexAnnotations([], ORDER)

    expect(index.byMessage.size).toBe(0)
    expect(index.unanchored).toEqual([])
  })
})
