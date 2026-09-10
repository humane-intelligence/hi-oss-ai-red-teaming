import type { NotificationResponse } from '@/lib/api/types'

// Deep-link target for a notification's referenced object, or null when it has no
// object or the type has no route yet. Keeps the type→route map in one place.
export function objectHref(
  notification: Pick<NotificationResponse, 'object_type' | 'object_id'>,
): string | null {
  const { object_type, object_id } = notification
  if (!object_id) return null
  switch (object_type) {
    case 'evaluation':
      return `/evaluations/${object_id}`
    case 'evaluation_group':
      return `/evaluation-groups/${object_id}`
    case 'ai_model':
      return `/ai-models/${object_id}`
    default:
      return null
  }
}
