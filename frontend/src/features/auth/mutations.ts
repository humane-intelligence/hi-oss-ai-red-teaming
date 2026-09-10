import { useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { ApiError } from '@/lib/api/problem'

// Re-issue the verification link for a pending self-signup (anonymous, enumeration-safe 202 —
// the toast copy stays state-agnostic on purpose: a 202 doesn't confirm the account exists).
export function useResendVerification() {
  return useMutation({
    mutationFn: async (email: string) =>
      unwrap(await apiClient.POST('/api/v1/auth/register/resend', { body: { email } })),
    onSuccess: () => {
      toast.success(
        'If the address has a pending sign-up, a fresh link is on its way — earlier links stop working',
      )
    },
    onError: (error) => {
      // The global handler skips a 422 / errors[] assuming the caller maps it onto a form —
      // this mutation has no form, so that slice owes its own fallback toast.
      if (
        error instanceof ApiError &&
        error.status !== 401 &&
        (error.status === 422 || !!error.problem.errors?.length)
      ) {
        toast.error('Could not resend the link. Please try again.')
      }
    },
  })
}
