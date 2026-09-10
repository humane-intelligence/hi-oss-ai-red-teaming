import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type {
  BulkForceLogoutRequest,
  BulkInviteRequest,
  BulkPasswordResetRequest,
  BulkUserStatusRequest,
  SettableUserStatus,
  UserUpdate,
} from '@/lib/api/types'

export function useUpdateUser(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: UserUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/auth/users/{user_id}', {
          params: { path: { user_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      qc.invalidateQueries({ queryKey: ['user', id] })
      toast.success('User updated')
    },
  })
}

// Both directions move the same caches: the account's group memberships and organization
// ride along with the tombstone, so the surfaces that project a member's identity are
// stale on delete exactly as much as on restore. Exported for the self-service rename,
// which stales the same identity projections.
export function useInvalidateUser() {
  const qc = useQueryClient()
  return (id: string) => {
    qc.invalidateQueries({ queryKey: ['users'] })
    // Stale-only for the detail: an eager refetch would 404-toast the still-mounted edit
    // page a delete is navigating away from; the next mount refetches.
    qc.invalidateQueries({ queryKey: ['user', id], refetchType: 'none' })
    qc.invalidateQueries({ queryKey: ['evaluation-group-members'] })
    qc.invalidateQueries({ queryKey: ['organization-members'] })
  }
}

export function useRestoreUser() {
  const invalidate = useInvalidateUser()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/auth/users/{user_id}/restore', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate(id)
      // The contract's caveat, surfaced where the admin acts: the delete revoked nothing.
      toast.success('User restored', {
        description:
          'Unexpired sessions from before the delete work again — force logout if unwanted.',
      })
    },
  })
}

export function useDeleteUser() {
  const invalidate = useInvalidateUser()
  const restore = useRestoreUser()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/auth/users/{user_id}', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate(id)
      let undone = false
      toast.success('User deleted', {
        description: restorableFromHint('Recently deleted on Users'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would restore an already-live row and toast its 409.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(id, { onError: () => (undone = false) })
          },
        },
      })
    },
  })
}

export function useForceLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/auth/users/{user_id}/force-logout', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      toast.success('User signed out')
    },
  })
}

export function useBulkForceLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: BulkForceLogoutRequest) =>
      unwrap(await apiClient.POST('/api/v1/auth/users/force-logout', { body })),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['users'] })
      const msg = `${data.succeeded} of ${data.total} users signed out`
      if (data.failed > 0) toast.warning(`${msg}, ${data.failed} failed`)
      else toast.success(msg)
    },
  })
}

export function useChangeUserStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, status }: { id: string; status: SettableUserStatus }) =>
      unwrap(
        await apiClient.POST('/api/v1/auth/users/{user_id}/status', {
          params: { path: { user_id: id } },
          body: { status },
        }),
      ),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['users'] })
      qc.invalidateQueries({ queryKey: ['user', vars.id] })
      toast.success(vars.status === 'active' ? 'User activated' : 'User deactivated')
    },
  })
}

export function useBulkChangeUserStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: BulkUserStatusRequest) =>
      unwrap(await apiClient.POST('/api/v1/auth/users/status', { body })),
    onSuccess: (data, vars) => {
      qc.invalidateQueries({ queryKey: ['users'] })
      // Every requested row, not only the ok ones: from the list page no ['user', id] query is
      // active, so these just mark cached entries stale and cost no refetch.
      for (const row of vars.rows) qc.invalidateQueries({ queryKey: ['user', row.data.user_id] })
      // The verb comes off the first row: the envelope allows a status per row, this UI only ever
      // sends one for the whole selection, and "updated" reads the same for both directions.
      const verb = vars.rows[0]?.data.status === 'inactive' ? 'deactivated' : 'activated'
      const msg = `${data.succeeded} of ${data.total} accounts ${verb}`
      if (data.failed > 0) toast.warning(`${msg}, ${data.failed} failed`)
      else toast.success(msg)
    },
  })
}

// The two reset hooks invalidate nothing: mailing a link changes no field the users list shows.
export function useSendPasswordReset() {
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/auth/users/{user_id}/password-reset', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: () => toast.success('Password reset link sent'),
  })
}

export function useBulkSendPasswordReset() {
  return useMutation({
    mutationFn: async (body: BulkPasswordResetRequest) =>
      unwrap(await apiClient.POST('/api/v1/auth/users/password-reset', { body })),
    onSuccess: (data) => {
      const msg = `${data.succeeded} of ${data.total} reset links sent`
      if (data.failed > 0) toast.warning(`${msg}, ${data.failed} failed`)
      else toast.success(msg)
    },
  })
}

export function useResendInvitation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/auth/users/{user_id}/invitation/resend', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      toast.success('Invitation resent')
    },
  })
}

export function useRevokeInvitation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/auth/users/{user_id}/invitation', {
          params: { path: { user_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      toast.success('Invitation revoked')
    },
  })
}

export function useBulkInvite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: BulkInviteRequest) =>
      unwrap(await apiClient.POST('/api/v1/auth/invitations/bulk', { body })),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['users'] })
      const msg = `${data.succeeded} of ${data.total} invitations sent`
      if (data.failed > 0) toast.warning(`${msg}, ${data.failed} failed`)
      else toast.success(msg)
    },
  })
}
