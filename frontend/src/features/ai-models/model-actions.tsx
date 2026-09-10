import { Activity, KeyRound, Pencil, Power, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { PageActions } from '@/components/shared/page-actions'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { AiModelResponse } from '@/lib/api/types'

export function ModelActions({
  model,
  onEdit,
  onSetApiKey,
  onToggleDisabled,
  onHealthCheck,
  onClearKey,
  onDelete,
  togglePending,
  healthPending,
  clearKeyPending,
}: {
  model: AiModelResponse
  onEdit: () => void
  onSetApiKey: () => void
  onToggleDisabled: () => void
  onHealthCheck: () => void
  onClearKey: () => void
  onDelete: () => void
  togglePending?: boolean
  healthPending?: boolean
  clearKeyPending?: boolean
}) {
  const { has } = usePermissions()
  const canUpdate = has('models:update')

  return (
    <PageActions
      primary={
        canUpdate && (
          <Button size="sm" onClick={onEdit}>
            <Pencil /> Edit
          </Button>
        )
      }
      secondary={[
        {
          key: 'api-key',
          label: 'Set API key',
          icon: KeyRound,
          onSelect: onSetApiKey,
          when: canUpdate,
        },
        {
          key: 'toggle',
          label: model.is_disabled ? 'Enable' : 'Disable',
          icon: Power,
          onSelect: onToggleDisabled,
          disabled: togglePending,
          when: canUpdate,
        },
        {
          key: 'health',
          label: model.health_check_status === 'checking' ? 'Checking…' : 'Health check',
          icon: Activity,
          onSelect: onHealthCheck,
          // Disabled only while the POST is in flight: a `checking` row that never settles (worker
          // down / task lost) must stay retriable, since the backend overrides a check older than
          // its stale TTL on the next POST.
          disabled: healthPending,
          when: canUpdate,
        },
        {
          key: 'clear-key',
          label: 'Clear key',
          onSelect: onClearKey,
          disabled: clearKeyPending,
          when: canUpdate && model.has_api_key,
        },
        {
          key: 'delete',
          label: 'Delete',
          icon: Trash2,
          onSelect: onDelete,
          destructive: true,
          when: has('models:delete'),
        },
      ]}
    />
  )
}
