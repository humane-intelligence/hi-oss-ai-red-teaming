import { useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { useOrganizations, type OrganizationOrderBy } from './queries'
import { useRestoreOrganization } from './mutations'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { Pagination } from '@/components/shared/pagination'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { OrganizationResponse } from '@/lib/api/types'

const PAGE_SIZE = 20
type OrgFilters = { deleted: boolean }
const DEFAULT_FILTERS: OrgFilters = { deleted: false }
const DEFAULT_ORDER_BY = 'name'

const columns: Column<OrganizationResponse>[] = [
  {
    id: 'name',
    label: 'Name',
    header: 'Name',
    cell: (o) => <span className="font-medium">{o.name}</span>,
    sortKey: 'name',
  },
  {
    id: 'description',
    label: 'Description',
    header: 'Description',
    hideBelow: 'md',
    cell: (o) => (
      <span className="text-muted-foreground line-clamp-1 max-w-md">{o.description || '—'}</span>
    ),
  },
  {
    id: 'created_at',
    label: 'Created',
    header: 'Created',
    cell: (o) => (
      <span className="text-muted-foreground">{new Date(o.created_at).toLocaleDateString()}</span>
    ),
    sortKey: 'created_at',
  },
]

export function OrganizationsListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const view = useListViewState<OrgFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: DEFAULT_ORDER_BY,
  })

  // A saved view keeps `deleted` in its filter blob, so gate the whole mode rather than
  // just the toggle — a caller who has since lost `organizations:delete` would otherwise
  // land on dead Restore buttons with no control to leave.
  const viewingDeleted = view.filters.deleted && has('organizations:delete')
  const query = useOrganizations({
    limit: PAGE_SIZE,
    offset: view.offset,
    name: view.search,
    order_by: (view.orderBy ?? DEFAULT_ORDER_BY) as OrganizationOrderBy,
    deleted: viewingDeleted,
  })
  const page = query.data
  const restore = useRestoreOrganization()

  // A tombstone has no detail page, so the row action replaces navigation.
  const deletedColumns: Column<OrganizationResponse>[] = [
    // Action first: on a tombstone row it is the only affordance (there is no detail page to
    // open), and a trailing column is the first thing a narrow viewport puts behind a scroll.
    {
      id: 'restore',
      header: '',
      cell: (o) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore organization: ${o.name}`}
          disabled={restore.isPending && restore.variables === o.id}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(o.id)
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
      cell: (o) => (
        <span className="text-muted-foreground">
          {o.deleted_at ? new Date(o.deleted_at).toLocaleString() : '—'}
        </span>
      ),
      sortKey: 'deleted_at',
    },
  ]

  const newButton = has('organizations:create') ? (
    <Button onClick={() => navigate('/organizations/new')}>
      <Plus className="size-4" /> New organization
    </Button>
  ) : undefined

  return (
    <div className="space-y-6">
      <PageHeader
        title="Organizations"
        description="Tenants that own evaluation groups and group their members."
      />
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
            placeholder="Search by name…"
            aria-label="Search organizations"
            className="min-w-0 flex-1"
          />
          <Button type="submit" variant="outline">
            Search
          </Button>
        </form>
        {has('organizations:delete') && (
          <DeletedToggle
            value={view.filters.deleted}
            onChange={(next) => {
              view.setFilter('deleted', next)
              // Newest tombstone first entering, as the `deleted` param's hint recommends.
              // Leaving, only a `deleted_at` sort is reset — it names a column the live
              // table doesn't have, while any other sort is still meaningful there.
              view.setOrderBy(
                next
                  ? '-deleted_at'
                  : view.orderBy?.endsWith('deleted_at')
                    ? DEFAULT_ORDER_BY
                    : (view.orderBy ?? DEFAULT_ORDER_BY),
              )
            }}
            label="Which organizations to show"
          />
        )}
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="organizations" view={view} columns={columns} />
          {!viewingDeleted && newButton}
        </div>
      </div>
      <DataTable
        columns={viewingDeleted ? deletedColumns : columns}
        rows={page?.items}
        rowKey={(o) => o.id}
        onRowClick={viewingDeleted ? undefined : (o) => navigate(`/organizations/${o.id}`)}
        isLoading={query.isPending || query.isPlaceholderData}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel={viewingDeleted ? 'Nothing deleted recently.' : 'No organizations yet.'}
        emptyHint={
          viewingDeleted
            ? 'Deleted organizations stay here for a limited time, then stop being restorable.'
            : 'Organizations are tenants you can assign users and evaluation groups to.'
        }
        emptyAction={viewingDeleted ? undefined : newButton}
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
