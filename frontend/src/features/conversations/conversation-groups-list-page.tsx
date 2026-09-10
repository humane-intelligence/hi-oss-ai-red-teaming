import { useNavigate } from 'react-router-dom'
import { useAllConversationGroups } from './queries'
import { useEvaluationLookup } from '@/features/evaluations/queries'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import type { ConversationGroupResponse } from '@/lib/api/types'

const PAGE_SIZE = 20
const DEFAULT_FILTERS: Record<string, never> = {}

export function ConversationGroupsListPage() {
  const navigate = useNavigate()
  const evaluationName = useEvaluationLookup()
  const view = useListViewState({ defaultFilters: DEFAULT_FILTERS })
  const query = useAllConversationGroups({ limit: PAGE_SIZE, offset: view.offset })
  const page = query.data

  const columns: Column<ConversationGroupResponse>[] = [
    {
      id: 'name',
      label: 'Name',
      header: 'Name',
      cell: (g) => <span className="font-medium">{g.name}</span>,
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
    </div>
  )
}
