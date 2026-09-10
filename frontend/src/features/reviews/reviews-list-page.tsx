import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useReviews, type ReviewOrderBy } from './queries'
import { useRestoreReview } from './mutations'
import { VerdictDialog } from './verdict-dialog'
import { verdictSummary } from './format'
import { useUserLookup } from '@/features/users/queries'
import { useEvaluationLookup } from '@/features/evaluations/queries'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import { DataTable, type Column } from '@/components/shared/data-table'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { Pagination } from '@/components/shared/pagination'
import { StatusPill } from '@/components/shared/status-pill'
import { Button } from '@/components/ui/button'
import { FilterSelect } from '@/components/shared/filter-select'
import type { ReviewResponse, ReviewStatus } from '@/lib/api/types'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

const PAGE_SIZE = 20

type ReviewFilters = { status: '' | ReviewStatus; mine: boolean; deleted: boolean }
const DEFAULT_FILTERS: ReviewFilters = { status: '', mine: false, deleted: false }
const DEFAULT_ORDER_BY = '-created_at'

export function ReviewsListPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [verdictReview, setVerdictReview] = useState<ReviewResponse | null>(null)
  const view = useListViewState<ReviewFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: DEFAULT_ORDER_BY,
  })
  const lookup = useUserLookup()
  const evaluationName = useEvaluationLookup()
  const { has } = usePermissions()
  const mine = view.filters.mine
  // The tombstone list is already scoped to the caller's own unassignments (a break-glass
  // manager sees every actor's), so it needs no permission gate of its own — but the
  // Restore action does, and `reviews:delete` is what the endpoint checks.
  const viewingDeleted = view.filters.deleted && has('reviews:delete')
  const query = useReviews({
    limit: PAGE_SIZE,
    offset: view.offset,
    status: view.filters.status || undefined,
    reviewer_id: mine ? user?.id : undefined,
    deleted: viewingDeleted,
    order_by: (view.orderBy ?? DEFAULT_ORDER_BY) as ReviewOrderBy,
  })
  const page = query.data
  const restore = useRestoreReview()

  const columns: Column<ReviewResponse>[] = [
    {
      id: 'evaluation',
      label: 'Evaluation',
      header: 'Evaluation',
      cell: (r) => <span className="font-medium">{evaluationName(r.evaluation_id)}</span>,
    },
    {
      id: 'reviewer',
      label: 'Reviewer',
      header: 'Reviewer',
      hideBelow: 'md',
      cell: (r) => (
        <span className="text-muted-foreground">{r.reviewer_email ?? lookup(r.reviewer_id)}</span>
      ),
    },
    {
      id: 'status',
      label: 'Status',
      header: 'Status',
      cell: (r) => <StatusPill status={r.status} />,
    },
    {
      id: 'verdict',
      label: 'Verdict',
      header: 'Verdict',
      hideBelow: 'sm',
      cell: (r) =>
        r.status === 'pending' ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <span className="text-muted-foreground">{verdictSummary(r)}</span>
        ),
    },
    {
      id: 'created_at',
      label: 'Created',
      header: 'Created',
      hideBelow: 'lg',
      cell: (r) => (
        <span className="text-muted-foreground">{new Date(r.created_at).toLocaleDateString()}</span>
      ),
    },
  ]

  // A tombstoned assignment takes neither the verdict dialog nor the submission link as its
  // primary action — the restore is what reaches it.
  const deletedColumns: Column<ReviewResponse>[] = [
    // Action first: on a tombstone row it is the only affordance (there is no detail page to
    // open), and a trailing column is the first thing a narrow viewport puts behind a scroll.
    {
      id: 'restore',
      header: '',
      cell: (r) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore reviewer: ${r.reviewer_email ?? lookup(r.reviewer_id)}`}
          disabled={restore.isPending && restore.variables === r.id}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(r.id)
          }}
        >
          Restore
        </Button>
      ),
    },
    ...columns,
    {
      id: 'deleted_at',
      label: 'Unassigned',
      header: 'Unassigned',
      cell: (r) => (
        <span className="text-muted-foreground">
          {r.deleted_at ? new Date(r.deleted_at).toLocaleString() : '—'}
        </span>
      ),
      sortKey: 'deleted_at',
    },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <ToggleGroup
          type="single"
          value={mine ? 'mine' : 'all'}
          onValueChange={(v) => v && view.setFilter('mine', v === 'mine')}
          variant="outline"
          size="sm"
        >
          <ToggleGroupItem value="all">All reviewers</ToggleGroupItem>
          <ToggleGroupItem value="mine">Assigned to me</ToggleGroupItem>
        </ToggleGroup>
        <FilterSelect
          label="Status"
          value={view.filters.status}
          onChange={(v) => view.setFilter('status', v as '' | ReviewStatus)}
          allLabel="All statuses"
          options={[
            { value: 'pending', label: 'pending' },
            { value: 'approved', label: 'approved' },
            { value: 'rejected', label: 'rejected' },
          ]}
        />
        {has('reviews:delete') && (
          <DeletedToggle
            value={view.filters.deleted}
            onChange={(next) => {
              view.setFilter('deleted', next)
              // Newest tombstone first entering, as the `deleted` param's hint recommends;
              // leaving, only a `deleted_at` sort is reset — it names a column the live table
              // doesn't have, while any other sort still means something there.
              view.setOrderBy(
                next
                  ? '-deleted_at'
                  : view.orderBy?.endsWith('deleted_at')
                    ? DEFAULT_ORDER_BY
                    : (view.orderBy ?? DEFAULT_ORDER_BY),
              )
            }}
            label="Which reviews to show"
          />
        )}
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="reviews" view={view} columns={columns} />
        </div>
      </div>
      <DataTable
        columns={viewingDeleted ? deletedColumns : columns}
        rows={page?.items}
        rowKey={(r) => r.id}
        onRowClick={
          viewingDeleted
            ? undefined
            : has('reviews:update')
              ? (r) => setVerdictReview(r)
              : (r) => navigate(`/reviews/submissions/${r.message_flag_id}`)
        }
        isLoading={query.isPending || query.isPlaceholderData}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel={viewingDeleted ? 'Nothing unassigned recently.' : 'No reviews yet.'}
        emptyHint={
          viewingDeleted
            ? 'Unassigned reviews stay here for a limited time, then stop being restorable.'
            : 'Reviews appear once reviewers are assigned to flagged messages.'
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
      <VerdictDialog review={verdictReview} onClose={() => setVerdictReview(null)} />
    </div>
  )
}
