import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Trash2 } from 'lucide-react'
import { useAllConversationGroups } from './queries'
import { useDeleteConversationGroup } from './mutations'
import { useGroupAuthority } from './use-group-authority'
import { useEvaluationLookup } from '@/features/evaluations/queries'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { Badge } from '@/components/ui/badge'
import { RowActions } from '@/components/shared/row-actions'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { ConversationGroupResponse } from '@/lib/api/types'

const PAGE_SIZE = 20
const DEFAULT_FILTERS: Record<string, never> = {}

export function ConversationGroupsListPage() {
  const navigate = useNavigate()
  const evaluationName = useEvaluationLookup()
  const view = useListViewState({ defaultFilters: DEFAULT_FILTERS })
  const query = useAllConversationGroups({ limit: PAGE_SIZE, offset: view.offset })
  const page = query.data
  const { user } = useAuth()
  const { has } = usePermissions()
  const [deleteTarget, setDeleteTarget] = useState<ConversationGroupResponse | null>(null)

  const columns: Column<ConversationGroupResponse>[] = [
    {
      id: 'name',
      label: 'Name',
      header: 'Name',
      cell: (g) => (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{g.name}</span>
          {(g.conversations ?? []).every((c) => c.turn_count === 0) && (
            <Badge variant="neutral">No messages</Badge>
          )}
        </div>
      ),
    },
    {
      id: 'evaluation',
      label: 'Evaluation',
      header: 'Evaluation',
      cell: (g) => evaluationName(g.evaluation_id),
    },
    {
      id: 'models',
      label: 'Models',
      header: 'Models',
      hideBelow: 'sm',
      cell: (g) => {
        const n = g.conversations?.length ?? 0
        return (
          <span className="text-muted-foreground">
            {n} model{n === 1 ? '' : 's'}
          </span>
        )
      },
    },
    {
      id: 'created_at',
      label: 'Started',
      header: 'Started',
      hideBelow: 'md',
      cell: (g) => (
        <span className="text-muted-foreground">{new Date(g.created_at).toLocaleString()}</span>
      ),
    },
    {
      header: '',
      className: 'text-right',
      cell: (g) => {
        // The server's rule; the parent-group arm is checked last because it is the only one that fetches.
        if (g.user_id !== user?.id && !has('evaluation_groups:manage')) return null
        const actions = (
          <RowActions
            menuLabel={`Actions for ${g.name}`}
            actions={[
              {
                key: 'delete',
                label: 'Delete',
                ariaLabel: `Delete ${g.name}`,
                icon: Trash2,
                onSelect: () => setDeleteTarget(g),
                destructive: true,
              },
            ]}
          />
        )
        if (has('conversations:delete')) return actions
        return <GrantedInGroup evaluationId={g.evaluation_id}>{actions}</GrantedInGroup>
      },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="Conversations"
        description="Every session you've run, newest first. Open one to pick up the side-by-side run across models."
      />
      <div className="flex flex-wrap gap-2">
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="conversation-groups" view={view} columns={columns} />
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(g) => g.id}
        onRowClick={(g) => navigate(`/evaluations/${g.evaluation_id}/conversation-groups/${g.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel="No conversations yet."
        emptyHint="Start one from an evaluation to probe a model."
      />
      {page && (
        <Pagination
          offset={view.offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={view.setOffset}
        />
      )}
      {deleteTarget && (
        <DeleteGroupDialog group={deleteTarget} onClose={() => setDeleteTarget(null)} />
      )}
    </div>
  )
}

function GrantedInGroup({ evaluationId, children }: { evaluationId: string; children: ReactNode }) {
  // A failed read hides the button like a refusal; per-row toasts would be noise.
  const { granted } = useGroupAuthority(evaluationId, 'conversations:delete', {
    suppressErrorToast: true,
  })
  return granted ? children : null
}

function DeleteGroupDialog({
  group,
  onClose,
}: {
  group: ConversationGroupResponse
  onClose: () => void
}) {
  const del = useDeleteConversationGroup(group.evaluation_id)
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Delete conversation"
      description={`Delete this conversation and everything in it? ${REVERSIBLE_DELETE_NOTE}`}
      confirmLabel="Delete"
      destructive
      pending={del.isPending}
      onConfirm={() =>
        del.mutate(
          { groupId: group.id, conversationIds: (group.conversations ?? []).map((c) => c.id) },
          { onSuccess: onClose },
        )
      }
    />
  )
}
