// Humanize the backend's `domain.verb` audit action codes for display / the filter select.
// No coupling to the backend enum's *labels* — we derive the text from the code itself.

// Words rendered as upper-case acronyms rather than sentence-cased (e.g. `ai_model` → "AI model").
const ACRONYMS = new Set(['ai'])

export const titleCase = (segment: string) =>
  segment
    .split('_')
    .map((word, i) =>
      ACRONYMS.has(word)
        ? word.toUpperCase()
        : i === 0
          ? word.charAt(0).toUpperCase() + word.slice(1)
          : word,
    )
    .join(' ')

// 'evaluation_group.publish' -> 'Evaluation group · Publish'
export function humanizeAction(code: string): string {
  if (!code) return ''
  const [domain = '', ...rest] = code.split('.')
  const verb = rest.join('.')
  return verb ? `${titleCase(domain)} · ${titleCase(verb)}` : titleCase(domain)
}

// 'evaluation_group.publish' -> 'Evaluation group' (used to group the select's options)
export function actionDomain(code: string): string {
  return titleCase(code.split('.')[0] ?? '')
}

// Known action codes, mirroring the backend `AuditAction` catalog (app/core/audit/enums.py).
// The backend `action` filter is exact-match, so a curated list beats a free-text input.
// Rarely changes; keep in sync when the backend catalog grows.
export const AUDIT_ACTIONS: string[] = [
  'auth.login',
  'auth.login_failed',
  'auth.register',
  'auth.email_verified',
  'auth.credential_reset_requested',
  'auth.credential_reset_confirmed',
  'user.update',
  'user.delete',
  'user.force_logout',
  'user.status_change',
  'user.credential_reset',
  'organization.create',
  'organization.update',
  'organization.delete',
  'organization.member_add',
  'organization.member_remove',
  'member.add',
  'member.set_roles',
  'member.remove',
  'invitation.create',
  'invitation.accept',
  'invitation.resend',
  'invitation.revoke',
  'evaluation_group.create',
  'evaluation_group.draft',
  'evaluation_group.duplicate',
  'evaluation_group.update',
  'evaluation_group.submit',
  'evaluation_group.publish',
  'evaluation_group.finish',
  'evaluation_group.approve',
  'evaluation_group.request_changes',
  'evaluation_group.reject',
  'evaluation_group.join',
  'evaluation.create',
  'evaluation.duplicate',
  'evaluation.update',
  'evaluation.delete',
  'evaluation.approve',
  'evaluation.reject',
  'evaluation.tag_key_add',
  'evaluation.tag_key_remove',
  'evaluation.model_assign',
  'evaluation.model_update',
  'evaluation.model_unassign',
  'scenario.create',
  'scenario.update',
  'scenario.delete',
  'scenario.reorder',
  'task.create',
  'task.update',
  'task.delete',
  'ai_model.create',
  'ai_model.update',
  'ai_model.credential_set',
  'ai_model.credential_clear',
  'ai_model.credential_access',
  'ai_model.delete',
  'conversation.tags_update',
  'flag.create',
  'flag.update',
  'flag.delete',
  'review.assign',
  'review.verdict',
  'review.unassign',
  'export.create',
  'export.delete',
  'export.download',
  'export.ready',
  'export.failed',
  'data.read',
  'platform_settings.update',
  'terms.publish',
  'terms.accept',
]
