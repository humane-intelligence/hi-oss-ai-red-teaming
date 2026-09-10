import { Link, useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { useEvaluations, type EvaluationOrderBy } from './queries'
import { EvaluationStatusBadge } from './status-badge'
import { useGroupLookup } from '@/features/evaluation-groups/queries'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { FilterSelect } from '@/components/shared/filter-select'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { EvaluationResponse, EvaluationStatus } from '@/lib/api/types'

const EVALUATION_STATUSES: EvaluationStatus[] = [
  'new',
  'draft',
  'under_review',
  'rejected',
  'approved',
  'published',
  'completed',
]

const PAGE_SIZE = 20

type EvaluationFilters = { status: EvaluationStatus | '' }
const DEFAULT_FILTERS: EvaluationFilters = { status: '' }

export function EvaluationsListPage() {
  return (
    <div className="space-y-6">
      <PageHeader title="Evaluations" description="Red-teaming evaluations visible to you." />
      <EvaluationsContent />
    </div>
  )
}

// Toolbar + table only (no PageHeader) — reused as the "All evaluations" tab on the groups page.
export function EvaluationsContent() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const { resolve: groupName, isPending: groupsPending } = useGroupLookup()
  const view = useListViewState<EvaluationFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: '-created_at',
  })

  const query = useEvaluations({
    limit: PAGE_SIZE,
    offset: view.offset,
    search: view.search,
    status: view.filters.status || undefined,
    order_by: (view.orderBy ?? '-created_at') as EvaluationOrderBy,
  })
  const page = query.data

  const columns: Column<EvaluationResponse>[] = [
    {
      id: 'title',
      label: 'Title',
      header: 'Title',
      cell: (e) => <span className="font-medium">{e.title}</span>,
      sortKey: 'title',
    },
    {
      id: 'status',
      label: 'Status',
      header: 'Status',
      cell: (e) => <EvaluationStatusBadge status={e.status} />,
      sortKey: 'status',
    },
    {
      id: 'group',
      label: 'Group',
      header: 'Group',
      hideBelow: 'md',
      // No link while the names are still loading: the link's only accessible name is the group
      // name, so linking before it resolves ships a link the reader cannot tell apart from another.
      cell: (e) => {
        if (groupsPending) return <span className="text-muted-foreground">Loading…</span>
        const name = groupName(e.evaluation_group_id)
        // Unnamed is mostly a closed gate — the lookup is disabled without `evaluation_groups:read`,
        // which is also what guards the group's own page, so a link would lead to a permission wall.
        if (name === null) return <span className="text-muted-foreground">—</span>
        return (
          <Link
            to={`/evaluation-groups/${e.evaluation_group_id}`}
            onClick={(ev) => ev.stopPropagation()}
            className="text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
          >
            {name}
          </Link>
        )
      },
    },
    {
      id: 'models',
      label: 'Models',
      header: 'Models',
      hideBelow: 'sm',
      cell: (e) => e.models?.length ?? 0,
    },
    {
      id: 'created_at',
      label: 'Created',
      header: 'Created',
      hideBelow: 'md',
      cell: (e) => (
        <span className="text-muted-foreground">{new Date(e.created_at).toLocaleDateString()}</span>
      ),
      sortKey: 'created_at',
    },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            view.commitSearch()
          }}
          className="flex w-full gap-2 sm:max-w-sm sm:min-w-64 sm:flex-1"
        >
          <Input
            value={view.searchDraft}
            onChange={(e) => view.setSearchDraft(e.target.value)}
            placeholder="Search title/description…"
            aria-label="Search evaluations"
            className="min-w-0 flex-1"
          />
          <Button type="submit" variant="outline">
            Search
          </Button>
        </form>
        <FilterSelect
          label="Status"
          value={view.filters.status}
          onChange={(v) => view.setFilter('status', v as EvaluationStatus | '')}
          allLabel="All statuses"
          options={EVALUATION_STATUSES.map((s) => ({ value: s, label: s.replace(/_/g, ' ') }))}
        />
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="evaluations" view={view} columns={columns} />
          {has('evaluations:create') && (
            <Button onClick={() => navigate('/evaluations/new')}>
              <Plus className="size-4" /> New evaluation
            </Button>
          )}
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(e) => e.id}
        onRowClick={(e) => navigate(`/evaluations/${e.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel="No evaluations yet."
        emptyAction={
          has('evaluations:create') ? (
            <Button onClick={() => navigate('/evaluations/new')}>
              <Plus className="size-4" /> New evaluation
            </Button>
          ) : undefined
        }
        sort={{ by: view.orderBy, onChange: view.setOrderBy }}
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
