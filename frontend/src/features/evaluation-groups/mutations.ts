import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type {
  EvaluationGroupCreate,
  EvaluationGroupDraftCreate,
  EvaluationGroupUpdate,
  GroupInvitationBulkRequest,
  ObjectMemberCreate,
  ObjectMemberUpdate,
} from '@/lib/api/types'

function useInvalidateGroup(id?: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['evaluation-groups'] })
    if (id) qc.invalidateQueries({ queryKey: ['evaluation-group', id] })
  }
}

export function useCreateGroup() {
  const invalidate = useInvalidateGroup()
  return useMutation({
    mutationFn: async (body: EvaluationGroupCreate) =>
      unwrap(await apiClient.POST('/api/v1/evaluation-groups', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Group created')
    },
  })
}

// Save partial progress: only `title` is required; omitted fields stay null.
export function useSaveDraft() {
  const invalidate = useInvalidateGroup()
  return useMutation({
    mutationFn: async (body: EvaluationGroupDraftCreate) =>
      unwrap(await apiClient.POST('/api/v1/evaluation-groups/draft', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Draft saved')
    },
  })
}

// Copy a group into a fresh draft owned by the caller. `includeChildren`
// also deep-copies its evaluations/scenarios/tasks/model assignments.
export function useDuplicateGroup() {
  const invalidate = useInvalidateGroup()
  return useMutation({
    mutationFn: async ({
      sourceId,
      includeChildren,
    }: {
      sourceId: string
      includeChildren: boolean
    }) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/duplicate', {
          params: { path: { group_id: sourceId }, query: { include_children: includeChildren } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Group duplicated')
    },
  })
}

export function useUpdateGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: EvaluationGroupUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluation-groups/{group_id}', {
          params: { path: { group_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      // A group edit can change its data_license, which children resolve into their own
      // effective_license — refresh the evaluation and conversation views so a cached child
      // doesn't show a stale license (the group-detail embed is already covered by invalidate()).
      qc.invalidateQueries({ queryKey: ['evaluation'] })
      qc.invalidateQueries({ queryKey: ['evaluations'] })
      qc.invalidateQueries({ queryKey: ['conversation'] })
      qc.invalidateQueries({ queryKey: ['conversations'] })
      qc.invalidateQueries({ queryKey: ['conversation-group'] })
      qc.invalidateQueries({ queryKey: ['conversation-groups'] })
      toast.success('Group updated')
    },
  })
}

// `renderInline` opts out of the global error toast for a caller that shows the refusal itself —
// the submit gate names every gap at once, which belongs on the form the operator is looking at,
// not in a toast that auto-dismisses. One failure, one channel.
export function useSubmitGroup(id: string, options?: { renderInline?: boolean }) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    meta: options?.renderInline ? { suppressErrorToast: true } : undefined,
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/submit', {
          params: { path: { group_id: id } },
        }),
      ),
    // Settled, not success: a 400 refusal means the cached `publication_blockers` are
    // stale, so the readiness panel and the button would keep calling the group ready.
    onSettled: () => invalidate(),
    onSuccess: () => toast.success('Group submitted for approval'),
  })
}

export function usePublishGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/publish', {
          params: { path: { group_id: id } },
        }),
      ),
    onSettled: () => invalidate(),
    onSuccess: () => toast.success('Group published'),
  })
}

export function useFinishGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/finish', {
          params: { path: { group_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Group finished')
    },
  })
}

export function useJoinGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/join', {
          params: { path: { group_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Joined group')
    },
  })
}

export function useAddGroupMember(groupId: string, options?: { successMessage?: string }) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ObjectMemberCreate) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/members', {
          params: { path: { group_id: groupId } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['evaluation-group-members', groupId] })
      toast.success(options?.successMessage ?? 'Member added')
    },
  })
}

export function useBulkInviteToGroup(groupId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: GroupInvitationBulkRequest) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/invitations/bulk', {
          params: { path: { group_id: groupId } },
          body,
        }),
      ),
    onSuccess: (data) => {
      // `assigned` outcomes add existing accounts to the group immediately —
      // including the annotator candidate pool rendered in the same section.
      qc.invalidateQueries({ queryKey: ['evaluation-group-members', groupId] })
      qc.invalidateQueries({ queryKey: ['evaluation-group-annotators', groupId] })
      const msg = `${data.succeeded} of ${data.total} invitations succeeded`
      if (data.failed > 0) toast.warning(`${msg}, ${data.failed} failed`)
      else toast.success(msg)
    },
  })
}

export function useUpdateGroupMember(groupId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ user_id, ...body }: ObjectMemberUpdate & { user_id: string }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluation-groups/{group_id}/members/{user_id}', {
          params: { path: { group_id: groupId, user_id } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['evaluation-group-members', groupId] })
      toast.success('Member updated')
    },
  })
}

// Removing a member tombstones one assignment row per role held, and those rows carry no
// state beyond `(group, user, role)` — so the undo is a re-add, not a restore endpoint. It
// also re-validates the roles, which matters when one has since stopped being
// object-assignable: the grant is refused rather than silently revived.
export function useRemoveGroupMember(groupId: string) {
  const qc = useQueryClient()
  const add = useAddGroupMember(groupId, { successMessage: 'Member restored' })
  return useMutation({
    mutationFn: async ({ userId }: { userId: string; roleIds: string[] }) =>
      unwrap(
        await apiClient.DELETE('/api/v1/evaluation-groups/{group_id}/members/{user_id}', {
          params: { path: { group_id: groupId, user_id: userId } },
        }),
      ),
    onSuccess: (_data, { userId, roleIds }) => {
      qc.invalidateQueries({ queryKey: ['evaluation-group-members', groupId] })
      let undone = false
      toast.success('Member removed', {
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would re-add an already-live member and toast its 409.
          onClick: () => {
            if (undone) return
            undone = true
            add.mutate({ user_id: userId, role_ids: roleIds }, { onError: () => (undone = false) })
          },
        },
      })
    },
  })
}

export function useApproveGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/approve', {
          params: { path: { group_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Group approved')
    },
  })
}

// No body — the change-note field is a deferred backend feature.
export function useRequestChangesGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/request-changes', {
          params: { path: { group_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Changes requested')
    },
  })
}

export function useRejectGroup(id: string) {
  const invalidate = useInvalidateGroup(id)
  return useMutation({
    mutationFn: async (rejection_reason: string) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluation-groups/{group_id}/reject', {
          params: { path: { group_id: id } },
          body: { rejection_reason },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Group rejected')
    },
  })
}
