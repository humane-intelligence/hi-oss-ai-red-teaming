import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, Plus } from 'lucide-react'
import { useDeletedLicenses, useLicenses } from './queries'
import { useRestoreLicense } from './mutations'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { humanizeError } from '@/lib/api/problem'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { DataLicenseSummary } from '@/lib/api/types'

const columns: Column<DataLicenseSummary>[] = [
  {
    id: 'name',
    label: 'Name',
    header: 'Name',
    cell: (l) => <span className="font-medium">{l.name}</span>,
  },
  {
    id: 'spdx_id',
    label: 'SPDX',
    header: 'SPDX',
    cell: (l) => <span className="text-muted-foreground">{l.spdx_id ?? '—'}</span>,
  },
  {
    id: 'version',
    label: 'Version',
    header: 'Version',
    hideBelow: 'lg',
    cell: (l) => <span className="text-muted-foreground">{l.version ?? '—'}</span>,
  },
  {
    id: 'type',
    label: 'Type',
    header: 'Type',
    hideBelow: 'sm',
    cell: (l) => (
      <div className="flex gap-1.5">
        <Badge variant={l.is_curated ? 'secondary' : 'outline'}>
          {l.is_curated ? 'Curated' : 'Custom'}
        </Badge>
        {l.is_default && <Badge>Default</Badge>}
        {l.protects_conversation_data && <Badge variant="ok">protected</Badge>}
      </div>
    ),
  },
  {
    id: 'short_description',
    label: 'Description',
    header: 'Description',
    hideBelow: 'lg',
    cell: (l) => (
      <span className="text-muted-foreground line-clamp-1 max-w-md">
        {l.short_description || '—'}
      </span>
    ),
  },
]

export function LicensesListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const query = useLicenses()
  const [search, setSearch] = useState('')

  // The licence catalog is a small, bounded set (curated + a handful of user-authored), fetched whole;
  // the endpoint has no server-side search, so filter the loaded page by name/SPDX in the UI.
  const term = search.trim().toLowerCase()
  const rows = query.data?.items.filter(
    (l) =>
      !term ||
      l.name.toLowerCase().includes(term) ||
      (l.spdx_id?.toLowerCase().includes(term) ?? false),
  )

  const newButton = has('licenses:create') ? (
    <Button onClick={() => navigate('/licenses/new')}>
      <Plus className="size-4" /> New license
    </Button>
  ) : undefined

  return (
    <div className="space-y-6">
      <PageHeader
        title="Data licenses"
        description="The data licenses an evaluation can be set to — curated (managed in code) and user-authored."
      />
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by name or SPDX…"
          aria-label="Filter licenses"
          className="min-w-0 flex-1 sm:max-w-sm sm:min-w-64"
        />
        <div className="flex flex-wrap gap-2 sm:ml-auto">{newButton}</div>
      </div>
      {query.data && query.data.total > query.data.items.length && (
        <p className="text-muted-foreground text-xs">
          Showing the first {query.data.items.length} of {query.data.total} licenses.
        </p>
      )}
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(l) => l.id}
        onRowClick={(l) => navigate(`/licenses/${l.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        emptyLabel={term ? 'No licenses match your filter.' : 'No licenses yet.'}
        emptyHint="Curated licenses ship with the platform; add your own with New license."
        emptyAction={newButton}
      />
      {has('licenses:delete') && <DeletedLicenses />}
    </div>
  )
}

// A disclosure rather than the flags-style `DeletedToggle`: this page fetches the whole
// licence set under the same `['licenses']` key the picker uses, so swapping that key to
// tombstones would poison the picker's cache everywhere else on the page.
function DeletedLicenses() {
  const [open, setOpen] = useState(false)
  const query = useDeletedLicenses(open)
  const restore = useRestoreLicense()
  const deleted = query.data?.items ?? []

  return (
    <section className="border-t pt-3">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls="deleted-licenses"
        className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-sm font-medium"
      >
        {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        Recently deleted
        {open && query.isSuccess && ` (${query.data.total})`}
      </button>
      {open && (
        <div id="deleted-licenses" className="mt-2 space-y-2">
          <p className="text-muted-foreground text-xs">
            {/* A `licenses:manage` holder sees every author's tombstones here, not just their
                own — the endpoint lifts the scope exactly as it lifts the restore gate. */}
            Licenses you can restore, for a limited time after they were deleted. Curated licenses
            are managed in code and come back with the next sync instead.
          </p>
          {query.isSuccess && query.data.total > deleted.length && (
            <p className="text-muted-foreground text-xs">
              Showing the first {deleted.length} of {query.data.total}.
            </p>
          )}
          {query.isPending && <p className="text-muted-foreground text-sm">Loading…</p>}
          {query.isError && (
            <p className="text-destructive text-sm">
              Could not load deleted licenses: {humanizeError(query.error)}
            </p>
          )}
          {!query.isPending && !query.isError && deleted.length === 0 && (
            <p className="text-muted-foreground text-sm">Nothing deleted recently.</p>
          )}
          <ul className="space-y-2">
            {deleted.map((lic) => (
              <li
                key={lic.id}
                className="bg-card flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{lic.name}</p>
                  {lic.deleted_at && (
                    <p className="text-muted-foreground text-xs">
                      Deleted{' '}
                      <time dateTime={lic.deleted_at}>
                        {new Date(lic.deleted_at).toLocaleString()}
                      </time>
                    </p>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Restore license: ${lic.name}`}
                  disabled={restore.isPending}
                  onClick={() => restore.mutate(lic.id)}
                >
                  Restore
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
