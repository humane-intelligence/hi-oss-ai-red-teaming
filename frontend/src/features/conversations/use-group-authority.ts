import { useEvaluation } from '@/features/evaluations/queries'
import { useEvaluationGroup } from '@/features/evaluation-groups/queries'
import { useEffectivePermissions, usePermissions } from '@/lib/auth/use-permissions'

// Authority for a conversation surface reached by `evaluation_id` alone. The server accepts
// the permission from the JWT *or* from a role held on the evaluation's parent group, so the
// client has to resolve the group before it can answer — which is why the verdict arrives
// asynchronously and every consumer needs the same three states.
//
// It deliberately does not return the evaluation query it resolves the group from: pages call
// `useEvaluation` themselves for their own data (same query key, so no extra request), which
// keeps this an authority hook rather than the page's data layer.
//
// `granted` is the verdict for `permission`; `allows` answers for any other key on the same
// union (a page gates several affordances off one fetch). `failed` keeps a load error from
// reading as a refusal — a deleted or unreachable group is not a permission verdict — and
// `pending` is only true while the answer could still turn out to be yes.
export function useGroupAuthority(evaluationId: string, permission: string) {
  const global = usePermissions()
  const evaluation = useEvaluation(evaluationId)
  const parentGroup = useEvaluationGroup(evaluation.data?.evaluation_group_id ?? '')
  const { has: allows } = useEffectivePermissions(parentGroup.data?.user_permissions)
  const granted = allows(permission)
  const failed = !granted && (evaluation.isError || parentGroup.isError)
  return {
    allows,
    // Writes are owner-scoped *except* for the `evaluation_groups:manage` break-glass, which the
    // write services honour off the JWT (`caller_can_manage_groups`) — so this reads the global
    // set, not the union: holding the key in-group would not lift the owner predicate server-side.
    canManage: global.has('evaluation_groups:manage'),
    granted,
    failed,
    pending: !granted && !failed && (evaluation.isPending || parentGroup.isPending),
    error: evaluation.error ?? parentGroup.error,
    groupPermissions: parentGroup.data?.user_permissions,
  }
}
