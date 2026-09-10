// Naming the conversations of a group. A member is identified on screen by its title, or
// its model when it has none — and neither is guaranteed distinct: a masked evaluation
// collapses every model name to the same string, and the same model can be added twice.

type NamedMember = { title?: string | null; modelName: string }

const baseName = (member: NamedMember) => member.title || member.modelName

/**
 * Per-member name suffixes, aligned by index with `members`.
 *
 * A member whose displayed name is unique gets `''`; one that collides gets ` (n)` with
 * `n` its 1-based position in the group — the position, not a count among the duplicates,
 * so the number matches the pane's place in the row of panes.
 */
export function memberNameSuffixes(members: NamedMember[]): string[] {
  const occurrences = new Map<string, number>()
  for (const member of members) {
    const name = baseName(member)
    occurrences.set(name, (occurrences.get(name) ?? 0) + 1)
  }
  return members.map((member, i) =>
    (occurrences.get(baseName(member)) ?? 0) > 1 ? ` (${i + 1})` : '',
  )
}

/** What the member is called on screen. */
export const memberLabel = (member: NamedMember, suffix = '') => baseName(member) + suffix

/**
 * What a screen reader hears. Keeps the model name even when a title covers it visually
 * (there it is only a subtitle), so panes and rail rows can be told apart when read out.
 *
 * The suffix goes with the title, not after the parenthetical, so the visible label stays
 * a prefix of this — WCAG 2.5.3 label-in-name, which voice control relies on.
 */
export const memberA11yLabel = (member: NamedMember, suffix = '') =>
  member.title ? `${member.title}${suffix} (${member.modelName})` : member.modelName + suffix
