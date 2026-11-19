import { sentenceCase } from '@/lib/sentence-case'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { formatCalendarDate } from '@/lib/format-date'
import { Plus } from 'lucide-react'
import { useEvaluationGroups, type EvaluationGroupOrderBy } from './queries'
import { PublicationStatusBadge } from './status-badge'
import { useOrganizationLookup } from '@/features/organizations/queries'
import { EvaluationsContent } from '@/features/evaluations/evaluations-list-page'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { SearchForm } from '@/components/shared/search-form'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { Tabs, type TabItem } from '@/components/ui/tabs'
import { tabPanelProps } from '@/components/ui/tab-panel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { FilterSelect } from '@/components/shared/filter-select'
import { usePermissions } from '@/lib/auth/use-permissions'
import type {
  EvaluationGroupResponse,
  EvaluationGroupAccessLevel,
  PublicationStatus,
} from '@/lib/api/types'

const PUBLICATION_STATUSES: PublicationStatus[] = [
  'draft',
  'pending_approval',
  'changes_requested',
  'approved',
  'not_approved',
  'published',
  'inactive',
]

const PAGE_SIZE = 20

type GroupFilters = { status: PublicationStatus | ''; accessLevel: EvaluationGroupAccessLevel | '' }
const DEFAULT_FILTERS: GroupFilters = { status: '', accessLevel: '' }
// Most groups have no organization; the column stays one click away under Columns.
const DEFAULT_HIDDEN = ['organization']

// Built per-render so the Organization cell can resolve `organization_id` to a
// name via the lookup hook (only callable inside a component).
function buildColumns(orgName: (id: string) => string): Column<EvaluationGroupResponse>[] {
  return [
    {
      id: 'title',
      label: 'Title',
      header: 'Title',
      // A real link, not just its look: keyboard reach and "open in a new tab"; the row keeps its own
      // click. Untitled drafts stay text: several links all named "Untitled draft" can't be told apart.
      cell: (g) =>
        g.title ? (
          <Link
            to={`/evaluation-groups/${g.id}`}
            onClick={(ev) => ev.stopPropagation()}
            className="text-primary font-medium underline-offset-2 hover:underline"
          >
            {g.title}
          </Link>
        ) : (
          <span className="text-muted-foreground">Untitled draft</span>
        ),
      sortKey: 'title',
    },
    {
      id: 'status',
      label: 'Status',
      header: 'Status',
      cell: (g) => <PublicationStatusBadge status={g.status} />,
    },
    {
      id: 'access',
      label: 'Access',
      header: 'Access',
      hideBelow: 'md',
      cell: (g) => <Badge variant="outline">{sentenceCase(g.access_level)}</Badge>,
    },
    {
      id: 'organization',
      label: 'Organization',
      header: 'Organization',
      hideBelow: 'md',
      cell: (g) => (
        <span className="text-muted-foreground">
          {g.organization_id ? orgName(g.organization_id) : '—'}
        </span>
      ),
    },
    {
      id: 'start_date',
      label: 'Start',
      header: 'Start',
      hideBelow: 'sm',
      cell: (g) => (
        <span className="text-muted-foreground whitespace-nowrap">
          {g.start_date ? formatCalendarDate(g.start_date) : '—'}
        </span>
      ),
      sortKey: 'start_date',
    },
    {
      id: 'end_date',
      label: 'End',
      header: 'End',
      hideBelow: 'lg',
      cell: (g) => (
        <span className="text-muted-foreground whitespace-nowrap">
          {g.end_date ? formatCalendarDate(g.end_date) : '—'}
        </span>
      ),
      sortKey: 'end_date',
    },
  ]
}

const TABS: TabItem[] = [
  { value: 'groups', label: 'Groups' },
  { value: 'evaluations', label: 'All evaluations' },
]

export function EvaluationGroupsListPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'evaluations' ? 'evaluations' : 'groups'

  return (
    <div className="space-y-6">
      <PageHeader
        title="Evaluation Groups"
        description="An evaluation group bundles related evaluations — for example, testing one model across versions."
      />
      <Tabs tabs={TABS} value={tab} onChange={(v) => setParams(v === 'groups' ? {} : { tab: v })} />
      <div {...tabPanelProps(tab)}>
        {tab === 'groups' ? <GroupsContent /> : <EvaluationsContent />}
      </div>
    </div>
  )
}

// Toolbar + table only (no PageHeader) — the "Groups" tab body.
function GroupsContent() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const orgName = useOrganizationLookup()
  const columns = buildColumns(orgName)
  const view = useListViewState<GroupFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: '-created_at',
    defaultHiddenColumns: DEFAULT_HIDDEN,
  })

  // A manager sees every group (incl. others' private/draft); everyone else gets
  // the visibility-scoped set. `all_groups` is a 403 without the permission.
  const query = useEvaluationGroups({
    limit: PAGE_SIZE,
    offset: view.offset,
    search: view.search,
    status: view.filters.status || undefined,
    order_by: (view.orderBy ?? '-created_at') as EvaluationGroupOrderBy,
    access_level: view.filters.accessLevel || undefined,
    all_groups: has('evaluation_groups:manage') || undefined,
  })
  const page = query.data
  const filtered = !!view.search || Object.values(view.filters).some(Boolean)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SearchForm
          value={view.searchDraft}
          onChange={view.setSearchDraft}
          onSubmit={view.commitSearch}
          placeholder="Search title/description…"
          label="Search evaluation groups"
        />
        <FilterSelect
          label="Status"
          value={view.filters.status}
          onChange={(v) => view.setFilter('status', v as PublicationStatus | '')}
          allLabel="All statuses"
          options={PUBLICATION_STATUSES.map((s) => ({ value: s, label: sentenceCase(s) }))}
        />
        <FilterSelect
          label="Access"
          value={view.filters.accessLevel}
          onChange={(v) => view.setFilter('accessLevel', v as EvaluationGroupAccessLevel | '')}
          allLabel="All access levels"
          className="sm:w-44"
          options={[
            { value: 'public', label: 'Public' },
            { value: 'organization', label: 'Organization' },
            { value: 'invitation_only', label: 'Invitation only' },
          ]}
        />
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="evaluation-groups" view={view} columns={columns} />
          {has('evaluation_groups:create') && (
            <Button onClick={() => navigate('/evaluation-groups/new')}>
              <Plus className="size-4" /> New group
            </Button>
          )}
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(g) => g.id}
        onRowClick={(g) => navigate(`/evaluation-groups/${g.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        // A search or filter that matches nothing is not a first visit: no "yet", no create prompt.
        emptyLabel={
          filtered
            ? 'No evaluation groups match this search or filter.'
            : 'No evaluation groups yet.'
        }
        emptyHint={
          filtered
            ? undefined
            : 'A group sets up an engagement: its dates, who can join, and the evaluations to run.'
        }
        emptyAction={
          !filtered && has('evaluation_groups:create') ? (
            <Button onClick={() => navigate('/evaluation-groups/new')}>
              <Plus className="size-4" /> New group
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
