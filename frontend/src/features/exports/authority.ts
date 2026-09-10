// The in-group authority the server requires to export (`evaluation_groups:export`, or the
// `:manage` break-glass). Both detail pages gate the Export affordance on this same set so they
// never drift from the backend's `assert_export_authority`.
//
// Deliberately **not** `evaluation_groups:update`: that permission is delegable, so a custom
// in-group role handed out to let someone rename a group would otherwise carry every member's
// transcripts, flags and reviews with it.
export const EXPORT_AUTHORITY_PERMISSIONS = ['evaluation_groups:export', 'evaluation_groups:manage']
