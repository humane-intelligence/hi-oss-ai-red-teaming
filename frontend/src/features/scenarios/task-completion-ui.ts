// The scenario rail's completion contract. Types + the key helper live outside the
// component file because `react-refresh/only-export-components` rejects a non-component
// export beside `ScenarioRail`.

// One member conversation of a group, as the rollup rail needs it.
export type RailMember = {
  conversationId: string
  // Already resolved by the group page (`member-names.ts`), including the position suffix
  // that disambiguates colliding names — the panes are labelled from the same call, so a
  // row and the pane it refers to always read the same.
  label: string
  a11yLabel: string
  completedTaskIds: Set<string>
  // Check-off is owner-only on the backend and the `evaluation_groups:manage` break-glass
  // does not lift it, so a viewer offered the control would get a 404.
  canToggle: boolean
}

// Whether the roll-up numbers can be trusted yet. `loading` and `error` both mean "don't
// assert a count": a member still reading looks identical to one with nothing completed.
export type RollupCountsState = 'ready' | 'loading' | 'error'

// How the task list renders its completion state. `toggle` shows per-task checkboxes a
// red-teamer flips in their conversation; `rollup` shows a "K/N conversations" count per
// task, which — when the caller owns at least one member — expands into one row per
// member: a checkbox where they own it, a static state marker otherwise.
export type TaskCompletionUI =
  | {
      mode: 'toggle'
      completedTaskIds: Set<string>
      onToggle: (taskId: string, next: boolean) => void
      // `false` for a non-owner: the row stays focusable (activating it explains the block
      // via a toast) but is marked aria-disabled so it doesn't announce as actionable.
      canToggle: boolean
      // Task ids with a toggle in flight — only these rows disable, so the rest stay
      // operable and focus isn't yanked off an unrelated checkbox. Safe to allow
      // concurrent toggles because the optimistic update is per-task (see task-completions).
      togglingTaskIds: Set<string>
    }
  | {
      mode: 'rollup'
      members: RailMember[]
      onToggle: (conversationId: string, taskId: string, next: boolean) => void
      // Per (member, task) — build entries with `togglingKey` rather than by hand, so the
      // producer and the rail can't drift apart.
      togglingKeys: Set<string>
      countsState: RollupCountsState
      // Whether a roll-up may be shown at all — a separate question from whether the reads
      // have settled. Completion reads are author-scoped and authoring is owner-only, so a
      // member the caller doesn't own answers `[]` whatever its owner ticked; a count off
      // that would report their work as zero. False hides the numbers entirely while
      // `countsState` keeps describing the reads, so the caller's own rows stay honest.
      countsReadable: boolean
    }

export const togglingKey = (conversationId: string, taskId: string) => `${conversationId}:${taskId}`
