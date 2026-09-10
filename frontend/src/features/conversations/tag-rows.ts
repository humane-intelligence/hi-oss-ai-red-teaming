import type { ConversationUpdate } from '@/lib/api/types'

// Straight from the generated contract, so a widened value type breaks the compile here.
export type TagMap = NonNullable<ConversationUpdate['tags']>
export type TagRow = { id: string; key: string; value: string }

export type TagRowsResult = { ok: true; tags: TagMap } | { ok: false; error: string }

// The server's own emptiness test, mirrored: it strips control and invisible-format characters
// (`Cc`, `Cf`, `Co`, `Cs`) and collapses whitespace before deciding a value carries context, so a
// value of only zero-width spaces or soft hyphens is nothing to send. `trim()` alone disagrees with
// it — `'\u200b'.trim()` is not empty — which is why this is spelled out rather than inlined.
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}]/gu

// Coerced like `TagFoldPolicy.unsent` does: the annotation says `string`, but a map written by an import
// or direct SQL need not hold one, and this runs during the transcript's render.
function isBlankTagValue(value: string): boolean {
  return String(value).replace(INVISIBLE, '').trim() === ''
}

/** What both authoring surfaces need to know about a row set: how many rows carry a key (the cap
 *  and the counter are about tags, not rows) and whether an empty one is already waiting (the cap
 *  counts tags, so nothing else bounds the row list). Shared so the two surfaces can't disagree. */
export function tagRowStats(rows: readonly { key: string }[]): {
  tagCount: number
  hasBlankRow: boolean
} {
  return {
    tagCount: rows.filter((r) => r.key.trim()).length,
    hasBlankRow: rows.some((r) => !r.key.trim()),
  }
}

// Shared by the conversation dialog and the message composer: both author rows and both write a
// keyed map, so both need the same answer to "what does an unusable row mean".
//
// `stored` is the map already on the record (the composer authors only new rows, so it defaults to
// none). A row that reproduces a stored tag is passed through unjudged: the write is whole-map, and
// refusing it would make one legacy valueless tag block every unrelated edit to the same
// conversation. What such a tag does not do is reach the model — see `unsentTagKeys`.
export function tagsFromRows(
  rows: readonly { key: string; value: string }[],
  stored: TagMap = {},
): TagRowsResult {
  // A `Map`, not an object literal, because both halves of the collision check are wrong on one:
  // `k in tags` is true for every `Object.prototype` member — `constructor`, `toString`,
  // `hasOwnProperty` and 9 more, all legal under the backend's `[A-Za-z0-9_.-]{1,64}` key rule — so
  // each would report as a duplicate on its first use and block the send. And `tags['__proto__'] =`
  // hits the inherited setter, creating no own property, so that row would leave the payload
  // silently. `Object.fromEntries` defines own data properties, so `__proto__` survives as a key.
  const seen = new Map<string, string>()
  for (const [i, { key, value }] of rows.entries()) {
    const k = key.trim()
    if (!k) {
      // A row with something typed in it and no key would vanish into the map; an untouched one the
      // author added and left alone is not an edit, so only the first is worth stopping for.
      if (value.trim()) return { ok: false, error: `Row ${i + 1}: key is required` }
      continue
    }
    if (seen.has(k)) return { ok: false, error: `Duplicate key "${k}"` }
    // An unfilled value is skipped when the prompt block is rendered, so authoring one would store
    // and chip context the model never received — on a message, forever. Blankness, not equality, is
    // what grandfathers a stored one: clearing three stored spaces means the same thing as clearing
    // an empty value, and refusing that would be a message about a row the operator just emptied.
    if (isBlankTagValue(value) && !(Object.hasOwn(stored, k) && isBlankTagValue(stored[k] ?? ''))) {
      return { ok: false, error: `Row ${i + 1}: value is required` }
    }
    seen.set(k, value)
  }
  return { ok: true, tags: Object.fromEntries(seen) }
}

/** Keys the backend will not fold into the prompt, so a reader is not told a stored tag reached the
 *  model. Three reasons, all mirroring the server: tagging switched off for the evaluation drops
 *  every tag, a value that carries no text is nothing to send, and a restricted evaluation folds only
 *  the keys it currently allows.
 *
 *  `allowedKeys` is `null` for an unrestricted evaluation. While the allow-list is unsettled
 *  (`keysSettled` false) no key can be judged on the allow-list — an empty list is not proof that
 *  nothing is allowed — but a valueless tag is still not sent. */
export function unsentTagKeys(
  tags: TagMap,
  {
    tagsEnabled,
    allowedKeys,
    keysSettled,
  }: { tagsEnabled: boolean; allowedKeys: string[] | null; keysSettled: boolean },
): Set<string> {
  if (!tagsEnabled) return new Set(Object.keys(tags))
  const unsent = new Set<string>()
  for (const [key, value] of Object.entries(tags)) {
    if (isBlankTagValue(value)) unsent.add(key)
    else if (allowedKeys !== null && keysSettled && !allowedKeys.includes(key)) unsent.add(key)
  }
  return unsent
}
