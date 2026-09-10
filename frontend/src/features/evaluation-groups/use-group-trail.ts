import { useEvaluationGroup } from './queries'
import { useOrganization } from '@/features/organizations/queries'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { Crumb } from '@/components/shared/breadcrumbs'

// The identity of the evaluation group a page sits inside: the section root, the owning
// organization when there is one, then the group. Every page in the subtree shows the same line, so
// the way back to the group never depends on which page you reached it from.
export function useGroupTrail(groupId: string): {
  crumbs: Crumb[]
  org: { name: string; to: string } | null
  groupTitle: string | undefined
  isPending: boolean
} {
  const { has } = usePermissions()
  const group = useEvaluationGroup(groupId)
  const orgId = group.data?.organization_id ?? ''
  // The one organization by id, not the whole list: a list cannot name an organization past its own
  // limit, and the trail needs exactly one name.
  // A dangling organization_id must not toast on every page of the subtree: the trail simply drops
  // the crumb it cannot name.
  const organization = useOrganization(orgId, {
    enabled: has('organizations:read'),
    suppressErrorToast: true,
  })

  const groupTitle = group.data ? (group.data.title ?? 'Untitled draft') : undefined
  const orgName = organization.data?.name
  // Named or absent, never an id: the organization is a crumb only once it can be labelled.
  const org = orgId && orgName ? { name: orgName, to: `/organizations/${orgId}` } : null

  return {
    crumbs: [
      { label: 'Evaluation Groups', to: '/evaluation-groups' },
      ...(org ? [{ label: org.name, to: org.to }] : []),
      ...(groupTitle ? [{ label: groupTitle, to: `/evaluation-groups/${groupId}` }] : []),
    ],
    org,
    groupTitle,
    // Only the group's own fetch gates the trail. The organization is a decoration on it, so the
    // name the reader came for is never withheld waiting on one.
    // The disabled-query idiom: a query that never ran is `isPending` too, and that is not loading.
    isPending: group.isPending && group.fetchStatus !== 'idle',
  }
}
