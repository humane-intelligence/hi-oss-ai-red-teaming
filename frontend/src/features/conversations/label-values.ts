import type { ConversationGroupResponse } from '@/lib/api/types'

// category id → the tags picked there. `BARE` stands for the category picked on its own, which is
// all a category without tags takes; a category that is absent or empty is unset.
export type LabelValues = Record<string, string[]>
export const BARE = ''

// What the group holds now, in the same shape.
export function heldLabels(labels: ConversationGroupResponse['labels']): LabelValues {
  const out: LabelValues = {}
  for (const label of labels ?? []) (out[label.category_id] ??= []).push(label.value ?? BARE)
  return out
}

// The map a create or a PATCH sends. The set is replaced, so an unset category is simply left out;
// a category picked on its own goes as an empty list.
export function labelPayload(value: LabelValues): Record<string, string[]> {
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, picked]) => picked.length > 0)
      .map(([id, picked]) => [id, picked.filter((v) => v !== BARE)]),
  )
}
