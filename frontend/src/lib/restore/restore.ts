// Copy for the delete/restore surfaces. The retention window itself is server-side
// config (`RESTORE_WINDOW_DAYS`) and deliberately not published to clients, so none of
// this states a number — an operator can retune the window without the UI going stale.

// Where the item can be found again differs per entity, so the caller names its own
// surface: a message pointing at a view the user cannot reach is worse than a vague one.
export function restorableFromHint(where: string): string {
  return `Restorable for a limited time from ${where}.`
}

// Confirmation copy for a delete this release made reversible — these dialogs used to
// say "this cannot be undone", which is now the opposite of the truth.
//
// Only for entities with a browsable "Recently deleted" surface (ai-models, licenses,
// conversations, notes, roles, organizations, users, reviews). Where the delete
// toast's Undo is the *only* way back — saved views, scenarios, tasks, model assignments,
// group members — this promises a window that outlives the 4s toast delivering it; those
// dialogs point at the toast instead.
export const REVERSIBLE_DELETE_NOTE = 'You can restore it for a limited time afterwards.'
