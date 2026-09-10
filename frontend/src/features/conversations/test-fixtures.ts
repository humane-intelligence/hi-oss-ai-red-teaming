import { http, HttpResponse } from 'msw'

// The parent evaluation group, as the conversation pages read it for the caller's in-group
// authority (`user_permissions`). Every conversation page fetches it, so a suite that omits
// the handler runs its authority path against a failed request — which reads as "no in-group
// permissions" and quietly passes on the caller's global ones instead.
export function parentGroupHandler(groupId: string, userPermissions: string[] = []) {
  return http.get(`http://localhost/api/v1/evaluation-groups/${groupId}`, () =>
    HttpResponse.json({
      id: groupId,
      title: 'Engagement',
      description: '',
      status: 'approved',
      access_level: 'invitation_only',
      start_date: '2026-01-01',
      end_date: null,
      created_by_id: 'user-000-0000-0000-000000000000',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      evaluations: [],
      user_permissions: userPermissions,
    }),
  )
}
