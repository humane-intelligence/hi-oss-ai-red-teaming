import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type { OrganizationCreate, OrganizationMemberAdd, OrganizationUpdate } from '@/lib/api/types'

function useInvalidateOrganization() {
  const qc = useQueryClient()
  return (id?: string) => {
    qc.invalidateQueries({ queryKey: ['organizations'] })
    if (id) qc.invalidateQueries({ queryKey: ['organization', id] })
  }
}

export function useCreateOrganization() {
  const invalidate = useInvalidateOrganization()
  return useMutation({
    mutationFn: async (body: OrganizationCreate) =>
      unwrap(await apiClient.POST('/api/v1/organizations', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Organization created')
    },
  })
}

export function useUpdateOrganization(id: string) {
  const invalidate = useInvalidateOrganization()
  return useMutation({
    mutationFn: async (body: OrganizationUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/organizations/{organization_id}', {
          params: { path: { organization_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate(id)
      toast.success('Organization updated')
    },
  })
}

// Members ride along with the org in both directions: the delete leaves their
// `organization_id` pointing at the tombstone, so the embedded org they project goes
// blank and comes back — both caches have to be refreshed either way.
function useInvalidateOrganizationCascade() {
  const qc = useQueryClient()
  return (id: string) => {
    qc.invalidateQueries({ queryKey: ['organizations'] })
    // Stale-only for the detail: an eager refetch would 404-toast the still-mounted page
    // a delete is navigating away from; the next mount refetches. The update path keeps
    // the eager refetch via useInvalidateOrganization.
    qc.invalidateQueries({ queryKey: ['organization', id], refetchType: 'none' })
    qc.invalidateQueries({ queryKey: ['organization-lookup'] })
    qc.invalidateQueries({ queryKey: ['organization-members', id] })
    qc.invalidateQueries({ queryKey: ['users'] })
    qc.invalidateQueries({ queryKey: ['user'] })
  }
}

export function useRestoreOrganization() {
  const invalidate = useInvalidateOrganizationCascade()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/organizations/{organization_id}/restore', {
          params: { path: { organization_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate(id)
      toast.success('Organization restored')
    },
  })
}

export function useDeleteOrganization() {
  const invalidate = useInvalidateOrganizationCascade()
  const restore = useRestoreOrganization()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/organizations/{organization_id}', {
          params: { path: { organization_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate(id)
      let undone = false
      toast.success('Organization deleted', {
        description: restorableFromHint('Recently deleted on Organizations'),
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

// Assigning a member moves the user out of any prior organization (single-org
// model), so refresh the users caches too — their embedded org has changed.
export function useAddOrganizationMember(organizationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: OrganizationMemberAdd) =>
      unwrap(
        await apiClient.POST('/api/v1/organizations/{organization_id}/members', {
          params: { path: { organization_id: organizationId } },
          body,
        }),
      ),
    onSuccess: (_data, body) => {
      qc.invalidateQueries({ queryKey: ['organization-members', organizationId] })
      qc.invalidateQueries({ queryKey: ['users'] })
      // The reassigned user's embedded org changed — refresh their detail cache too.
      qc.invalidateQueries({ queryKey: ['user', body.user_id] })
      toast.success('Member added')
    },
  })
}

export function useRemoveOrganizationMember(organizationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (userId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/organizations/{organization_id}/members/{user_id}', {
          params: { path: { organization_id: organizationId, user_id: userId } },
        }),
      ),
    onSuccess: (_data, userId) => {
      qc.invalidateQueries({ queryKey: ['organization-members', organizationId] })
      qc.invalidateQueries({ queryKey: ['users'] })
      // The removed user's embedded org changed — refresh their detail cache too.
      qc.invalidateQueries({ queryKey: ['user', userId] })
      toast.success('Member removed')
    },
  })
}
