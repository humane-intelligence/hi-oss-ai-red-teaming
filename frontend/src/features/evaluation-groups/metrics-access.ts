import type { MetricsAccessLevel } from '@/lib/api/types'

// One list drives the form's zod enum + both selects and the detail page's label, so
// the wire value has exactly one user-facing text everywhere. `satisfies` ties the
// values to the generated union — a backend rename fails here at the definition site.
// ("members (own metrics only)" = members holding `evaluation_groups:view_personal_metrics`,
// i.e. red teamers, who see the dashboards filtered to their own submissions; the owner
// always sees the full aggregate.)
export const METRICS_ACCESS_VALUES = [
  'inherit_group_access',
  'all_members',
  'members_personal_metrics',
  'owner_only',
] as const satisfies readonly MetricsAccessLevel[]

export const METRICS_ACCESS_LABELS: Record<(typeof METRICS_ACCESS_VALUES)[number], string> = {
  inherit_group_access: 'everyone who can see the group',
  all_members: 'all members',
  members_personal_metrics: 'members (own metrics only)',
  owner_only: 'owner only',
}
