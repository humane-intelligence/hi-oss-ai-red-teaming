import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { RoleCreate, RoleUpdate } from '@/lib/api/types'

function useInvalidateRole() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['roles'] })
    // Every role entity, not only the mutated one: making a role a default clears the
    // previous holder's flag in the same request, and a delete leaves its own entry
    // cached as a live role. Both are one row-click away from the list.
    qc.invalidateQueries({ queryKey: ['role'] })
    // A rename/deactivation/delete changes the roles embedded in user and member listings.
    qc.invalidateQueries({ queryKey: ['users'] })
    qc.invalidateQueries({ queryKey: ['user'] })
    qc.invalidateQueries({ queryKey: ['evaluation-group-members'] })
  }
}

export function useCreateRole() {
  const invalidate = useInvalidateRole()
  return useMutation({
    mutationFn: async (body: RoleCreate) => unwrap(await apiClient.POST('/api/v1/roles', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Role created')
    },
  })
}

export function useUpdateRole(id: string) {
  const invalidate = useInvalidateRole()
  return useMutation({
    mutationFn: async (body: RoleUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/roles/{role_id}', {
          params: { path: { role_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Role updated')
    },
  })
}

export function useRestoreRole() {
  const invalidate = useInvalidateRole()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/roles/{role_id}/restore', {
          params: { path: { role_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      // Holders regain the permissions on their next token, not this instant — the same
      // rule the grant path follows, so the copy doesn't promise immediacy.
      toast.success('Role restored')
    },
  })
}

export function useDeleteRole() {
  const invalidate = useInvalidateRole()
  const restore = useRestoreRole()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/roles/{role_id}', { params: { path: { role_id: id } } }),
      ),
    onSuccess: (_data, id) => {
      invalidate()
      let undone = false
      toast.success('Role deleted', {
        description: restorableFromHint('Recently deleted on Roles'),
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
