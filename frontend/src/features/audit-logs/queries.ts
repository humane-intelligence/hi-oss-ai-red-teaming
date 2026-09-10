import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type { operations } from '@/lib/api/schema'

export type AuditLogOrderBy = NonNullable<
  operations['list_audit_logs_endpoint_api_v1_audit_logs_get']['parameters']['query']
>['order_by']

export function useAuditLogs(params: {
  limit: number
  offset: number
  action?: string
  created_from?: string
  created_to?: string
  order_by?: AuditLogOrderBy
}) {
  return useQuery({
    queryKey: ['audit-logs', params],
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/audit-logs', {
          params: {
            query: {
              limit: params.limit,
              offset: params.offset,
              order_by: params.order_by,
              ...(params.action && { action: params.action }),
              ...(params.created_from && { created_from: params.created_from }),
              ...(params.created_to && { created_to: params.created_to }),
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  })
}
