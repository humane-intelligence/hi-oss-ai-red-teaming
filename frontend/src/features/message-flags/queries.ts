import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { useEffectivePermissions, usePermissions } from '@/lib/auth/use-permissions'
import type { operations } from '@/lib/api/schema'
import type { FlagStatus } from '@/lib/api/types'

export type MessageFlagOrderBy = NonNullable<
  operations['list_message_flags_endpoint_api_v1_message_flags_get']['parameters']['query']
>['order_by']

// The server authorizes the group behind the filter and accepts the permission from a role held
// there as readily as from the JWT, but only the caller knows the object — so callers pass the
// group's `user_permissions` and the union is applied here rather than overriding `enabled`.
export function useMessageFlags(
  params: {
    limit: number
    offset: number
    status?: FlagStatus
    search?: string
    order_by?: MessageFlagOrderBy
    red_flagged?: boolean
    conversation_id?: string
    deleted?: boolean
  },
  options: { groupPermissions?: readonly string[] } = {},
) {
  const { has } = useEffectivePermissions(options.groupPermissions)
  return useQuery({
    queryKey: ['message-flags', params],
    enabled: params.conversation_id !== '' && has('flags:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/message-flags', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              status: params.status || undefined,
              search: params.search || undefined,
              order_by: params.order_by,
              conversation_id: params.conversation_id || undefined,
              ...(params.red_flagged !== undefined && { red_flagged: params.red_flagged }),
              ...(params.deleted && { deleted: true }),
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}

export function useMessageFlag(id: string) {
  return useQuery({
    queryKey: ['message-flag', id],
    enabled: id !== '',
    // The detail page renders both branches itself — a refusal as `NotAuthorized`, anything else
    // inline — so the global toast would report the same failure twice.
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/message-flags/{flag_id}', {
          params: { path: { flag_id: id } },
        }),
      ),
  })
}

// Reviews on one flag (the submission's outcome). Gated on reviews:read so an
// author without it falls back to the flag's own status instead of a 403 toast.
export function useFlagReviews(flagId: string) {
  const { has } = usePermissions()
  return useQuery({
    queryKey: ['reviews', { message_flag_id: flagId }],
    enabled: flagId !== '' && has('reviews:read'),
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/reviews', {
          params: { query: { message_flag_id: flagId, limit: 100, offset: 0 } },
        }),
      ),
  })
}
