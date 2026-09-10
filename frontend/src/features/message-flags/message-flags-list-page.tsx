import { Link, useNavigate } from 'react-router-dom'
import { useMessageFlags, type MessageFlagOrderBy } from './queries'
import { useRestoreFlag } from './mutations'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { StatusPill } from '@/components/shared/status-pill'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { FilterSelect } from '@/components/shared/filter-select'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { MessageFlagResponse, FlagStatus } from '@/lib/api/types'

const FLAG_STATUSES: FlagStatus[] = ['pending', 'approved', 'rejected']

type RedFlaggedOption = 'true' | 'false' | ''

const PAGE_SIZE = 20

type FlagFilters = { status: FlagStatus | ''; redFlagged: RedFlaggedOption; deleted: boolean }
const DEFAULT_FILTERS: FlagFilters = { status: '', redFlagged: '', deleted: false }
const DEFAULT_ORDER_BY = '-created_at'

const columns: Column<MessageFlagResponse>[] = [
  {
    id: 'reason',
    label: 'Reason',
    header: 'Reason',
    cell: (f) => <span className="line-clamp-1 max-w-md">{f.reason}</span>,
  },
  {
    id: 'status',
    label: 'Status',
    header: 'Status',
    cell: (f) => <StatusPill status={f.status} />,
  },
  {
    id: 'red_flagged',
    label: 'Red-flagged',
    header: 'Red-flagged',
    hideBelow: 'md',
    cell: (f) => (f.red_flagged ? 'Yes' : 'No'),
  },
  {
    id: 'messages',
    label: 'Messages',
    header: 'Messages',
    hideBelow: 'lg',
    cell: (f) => f.messages.length,
  },
  {
    id: 'conversation',
    label: 'Conversation',
    header: 'Conversation',
    hideBelow: 'lg',
    cell: (f) => (
      <Link
        to={`/evaluations/${f.evaluation_id}/conversations/${f.conversation_id}`}
        className="text-sm underline-offset-2 hover:underline"
        onClick={(e) => e.stopPropagation()}
      >
        Open
      </Link>
    ),
  },
  {
    id: 'created_at',
    label: 'Created',
    header: 'Created',
    hideBelow: 'sm',
    cell: (f) => (
      <span className="text-muted-foreground">{new Date(f.created_at).toLocaleDateString()}</span>
    ),
    sortKey: 'created_at',
  },
]

export function MessageFlagsListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const restore = useRestoreFlag()
  const view = useListViewState<FlagFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: DEFAULT_ORDER_BY,
  })

  // A saved view keeps `deleted` in its filter blob, so one saved while browsing
  // tombstones re-enters them later — gate the whole mode, not just the toggle, or a
  // caller who has since lost `flags:delete` lands on dead Restore buttons with no
  // control to leave.
  const viewingDeleted = view.filters.deleted && has('flags:delete')
  const redFlagged = view.filters.redFlagged
  const redFlaggedParam = redFlagged === 'true' ? true : redFlagged === 'false' ? false : undefined
  const query = useMessageFlags({
    limit: PAGE_SIZE,
    offset: view.offset,
    status: view.filters.status || undefined,
    search: view.search || undefined,
    order_by: (view.orderBy ?? DEFAULT_ORDER_BY) as MessageFlagOrderBy,
    red_flagged: redFlaggedParam,
    deleted: viewingDeleted,
  })
  const page = query.data
  // A tombstone has no readable detail page, so the row action replaces navigation —
  // but every identifying column stays: two flags can share a reason, and the
  // transcript link still resolves (a tombstoned flag doesn't delete its conversation).
  const deletedColumns: Column<MessageFlagResponse>[] = [
    // Action first: on a tombstone row it is the only affordance (there is no detail page to
    // open), and a trailing column is the first thing a narrow viewport puts behind a scroll.
    {
      id: 'restore',
      header: '',
      cell: (f) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore flag: ${f.reason}`}
          disabled={restore.isPending}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(f.id)
          }}
        >
          Restore
        </Button>
      ),
    },
    ...columns,
    {
      id: 'deleted_at',
      label: 'Deleted',
      header: 'Deleted',
      cell: (f) => (
        <span className="text-muted-foreground">
          {f.deleted_at ? new Date(f.deleted_at).toLocaleString() : '—'}
        </span>
      ),
      sortKey: 'deleted_at',
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="My flags"
        description="Responses you flagged as exploit-worthy, and where they stand."
      />
      <div className="flex flex-wrap gap-2">
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
            placeholder="Search reason…"
            aria-label="Search flags by reason"
            className="min-w-0 flex-1"
          />
          <Button type="submit" variant="outline">
            Search
          </Button>
        </form>
        <FilterSelect
          label="Status"
          value={view.filters.status}
          onChange={(v) => view.setFilter('status', v as FlagStatus | '')}
          allLabel="All statuses"
          options={FLAG_STATUSES.map((s) => ({ value: s, label: s.replace(/_/g, ' ') }))}
        />
        <FilterSelect
          label="Red-flagged"
          value={view.filters.redFlagged}
          onChange={(v) => view.setFilter('redFlagged', v as RedFlaggedOption)}
          allLabel="All flags"
          options={[
            { value: 'true', label: 'Red-flagged' },
            { value: 'false', label: 'Not red-flagged' },
          ]}
        />
        {has('flags:delete') && (
          <DeletedToggle
            value={view.filters.deleted}
            onChange={(next) => {
              view.setFilter('deleted', next)
              // Newest tombstone first entering (what the `deleted` param's own hint
              // recommends, and what both sibling surfaces pass). Leaving, only a
              // `deleted_at` token is reset: it names a column the live table doesn't
              // have, while any other sort the view carries is still meaningful there.
              view.setOrderBy(
                next
                  ? '-deleted_at'
                  : view.orderBy?.endsWith('deleted_at')
                    ? DEFAULT_ORDER_BY
                    : (view.orderBy ?? DEFAULT_ORDER_BY),
              )
            }}
            label="Which flags to show"
          />
        )}
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="message-flags" view={view} columns={columns} />
        </div>
      </div>
      <DataTable
        columns={viewingDeleted ? deletedColumns : columns}
        rows={page?.items}
        rowKey={(f) => f.id}
        onRowClick={viewingDeleted ? undefined : (f) => navigate(`/message-flags/${f.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel={viewingDeleted ? 'Nothing deleted recently.' : 'No flagged messages yet.'}
        emptyHint={
          viewingDeleted
            ? 'Deleted flags stay here for a limited time, then stop being restorable.'
            : "Flag a model's response inside a conversation to send it for review."
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
