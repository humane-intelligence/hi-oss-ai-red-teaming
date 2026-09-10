import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { usePermissions } from '@/lib/auth/use-permissions'

// The server maximum, and a single page: both reads below fetch one and stop. Annotations are one
// row per label per author, so a busy transcript reaches this far sooner than notes do. Neither
// read is allowed to pass off its page as the whole set — the hosts disclose the annotation
// shortfall (`LabelsTruncatedNote`), the dialog its own vocabulary's.
export const ANNOTATIONS_PAGE_SIZE = 100

// The labels this caller may pick from: the shared curated vocabulary, their own, and those
// already used in this conversation. One page, so a longer vocabulary is truncated — the dialog
// says so, since its search filters only what it holds. Cached briefly rather than hard — unlike
// a purely code-owned list, this one grows when anyone on the conversation names a label.
export function useAnnotationLabels(conversationId: string, enabled: boolean) {
  const { has } = usePermissions()
  // `conversation_id` widens the offer to labels other annotators already used on this
  // transcript, so two people labelling it converge on one spelling.
  const params = { conversation_id: conversationId, limit: ANNOTATIONS_PAGE_SIZE, offset: 0 }
  return useQuery({
    queryKey: ['annotation-labels', params],
    // The empty id would serialise as `conversation_id=` and 422; the dialog can mount
    // before its host page has resolved the conversation.
    enabled: enabled && conversationId !== '' && has('annotations:read'),
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/annotation-labels', { params: { query: params } })),
    staleTime: 60 * 1000,
    meta: { suppressErrorToast: true },
  })
}

// Every author's annotations on one conversation — reads are deliberately shared server-side,
// unlike notes, because a label exists to be counted across annotators. Gated on
// `annotations:read` because the red teamer holds no annotation key at all and would
// otherwise eat a 403 on every transcript they open.
export function useConversationAnnotations(conversationId: string) {
  const { has } = usePermissions()
  // `order_by` spelled out though it matches the server default: the truncation note claims the
  // page holds the *most recent* labels, so the ordering that claim rests on has to be ours.
  const params = {
    conversation_id: conversationId,
    order_by: '-created_at' as const,
    limit: ANNOTATIONS_PAGE_SIZE,
    offset: 0,
  }
  return useQuery({
    queryKey: ['annotations', params],
    enabled: conversationId !== '' && has('annotations:read'),
    queryFn: async () =>
      unwrap(await apiClient.GET('/api/v1/annotations', { params: { query: params } })),
    // Both host pages render this failure inline (a silent read would have the annotator
    // conclude the message is unlabelled and label it again), so the global toast would
    // say it twice.
    meta: { suppressErrorToast: true },
  })
}
