import { useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { useAuth } from '@/lib/auth/auth-context'
import { useInvalidateUser } from '@/features/users/mutations'
import type { MeUpdate } from '@/lib/api/types'

export function useUpdateMe() {
  const { updateUser } = useAuth()
  const invalidate = useInvalidateUser()
  return useMutation({
    mutationFn: async (body: MeUpdate) =>
      unwrap(await apiClient.PATCH('/api/v1/auth/me', { body })),
    // The card renders every failure itself — inline on its own fields, else as a
    // form-level error — so it owns the whole channel rather than sharing it with the toast.
    meta: { suppressErrorToast: true },
    onSuccess: (me) => {
      // The held identity lives in AuthProvider state (adopt the response); every cache
      // that projects this user's identity — users list/detail, group and org members —
      // is covered by the shared helper.
      updateUser(me)
      invalidate(me.id)
      toast.success('Profile updated')
    },
  })
}
