import { useMemo } from 'react'
import { useAuditLogs, type AuditLogOrderBy } from './queries'
import { AUDIT_ACTIONS, actionDomain, humanizeAction, titleCase } from './labels'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import {
  EMPTY_VALUE,
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { DateTimeRange } from './date-time-range'
import type { AuditLogResponse } from '@/lib/api/types'

const ACTION_LABEL_ID = 'audit-action-label'
const ACTION_TRIGGER_ID = 'audit-action'

const PAGE_SIZE = 20

type AuditLogFilters = {
  action: string
  fromDate: string
  fromTime: string
  toDate: string
  toTime: string
}
// Module-scope (stable ref — required by useListViewState); values stay primitive. The time fields
// carry a default (start/end of day) so picking a date alone filters without touching the time.
const DEFAULT_FILTERS: AuditLogFilters = {
  action: '',
  fromDate: '',
  fromTime: '00:00',
  toDate: '',
  toTime: '23:59',
}

// Combine a `YYYY-MM-DD` date + `HH:mm` time (both local) into a UTC instant for the backend range
// filter. No date => no bound. An empty time falls back to the day boundary, so a date alone works:
// `from` seconds to :00 (start of the minute/midnight), `to` to :59.999 (end of the minute/day, inclusive).
const startIso = (date: string, time: string) =>
  date ? new Date(`${date}T${time || '00:00'}:00`).toISOString() : undefined
const endIso = (date: string, time: string) =>
  date ? new Date(`${date}T${time || '23:59'}:59.999`).toISOString() : undefined

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground mb-1 text-xs font-medium">{label}</div>
      <pre className="bg-background overflow-x-auto rounded p-2 text-xs">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}

function hasContext(ctx: AuditLogResponse['context']) {
  return ctx != null && Object.keys(ctx).length > 0
}

function renderExpanded(row: AuditLogResponse) {
  const showBeforeAfter = row.before != null || row.after != null
  if (!showBeforeAfter && !hasContext(row.context) && !row.request_id && !row.object_id) return null
  return (
    <div className="space-y-3">
      {showBeforeAfter && (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-3 sm:grid-cols-2">
          <JsonBlock label="Before" value={row.before ?? null} />
          <JsonBlock label="After" value={row.after ?? null} />
        </div>
      )}
      {hasContext(row.context) && <JsonBlock label="Context" value={row.context} />}
      {(row.object_id || row.request_id) && (
        <div className="text-muted-foreground flex flex-wrap gap-x-6 gap-y-0.5 text-xs">
          {row.object_id && (
            <div>
              Object ID: <span className="font-mono">{row.object_id}</span>
            </div>
          )}
          {row.request_id && (
            <div>
              Request ID: <span className="font-mono">{row.request_id}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const actorLabel = (r: AuditLogResponse) =>
  r.actor_email ? r.actor_email : <span className="text-muted-foreground">— system —</span>

const columns: Column<AuditLogResponse>[] = [
  {
    id: 'time',
    label: 'Time',
    header: 'Time',
    sortKey: 'created_at',
    cell: (r) => {
      const at = new Date(r.created_at)
      return (
        <>
          {/* The full stamp wants ~150px, which is most of a phone's row. Day and time are enough
              to place an entry when the list is already filtered to a range. */}
          {/* Wraps rather than nowrap: at 320px the stamp and the action together need the give. */}
          <span className="sm:hidden">
            {at.toLocaleString(undefined, {
              day: 'numeric',
              month: 'short',
              hour: '2-digit',
              minute: '2-digit',
            })}
          </span>
          <span className="hidden whitespace-nowrap sm:inline">{at.toLocaleString()}</span>
        </>
      )
    },
  },
  {
    id: 'actor',
    label: 'Actor',
    header: 'Actor',
    hideBelow: 'md',
    cell: (r) => actorLabel(r),
  },
  {
    id: 'action',
    label: 'Action',
    header: 'Action',
    // Never dropped: it is what the entry *is*, and this table has no detail view to fall back on.
    cell: (r) => (
      <div className="min-w-0">
        <span title={r.action}>{humanizeAction(r.action)}</span>
        {/* Carries the actor while its own column is hidden, so who/what/when all survive. */}
        <span className="text-muted-foreground block truncate text-xs md:hidden">
          {actorLabel(r)}
        </span>
      </div>
    ),
  },
  {
    id: 'object',
    label: 'Object',
    header: 'Object',
    hideBelow: 'lg',
    cell: (r) =>
      r.object_type ? (
        <span title={r.object_id ?? undefined}>{titleCase(r.object_type)}</span>
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
]

export function AuditLogsListPage() {
  const view = useListViewState<AuditLogFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: '-created_at',
  })

  // Group the action catalog by domain for the select's optgroups (better scanning across ~60 codes).
  const actionGroups = useMemo(() => {
    const groups = new Map<string, string[]>()
    for (const code of AUDIT_ACTIONS) {
      const domain = actionDomain(code)
      groups.set(domain, [...(groups.get(domain) ?? []), code])
    }
    return [...groups.entries()]
  }, [])

  const query = useAuditLogs({
    limit: PAGE_SIZE,
    offset: view.offset,
    action: view.filters.action || undefined,
    created_from: startIso(view.filters.fromDate, view.filters.fromTime),
    created_to: endIso(view.filters.toDate, view.filters.toTime),
    order_by: (view.orderBy ?? '-created_at') as AuditLogOrderBy,
  })
  const page = query.data

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit Log"
        description="Append-only record of sensitive actions and accesses."
      />
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <span id={ACTION_LABEL_ID} className="sr-only">
          Action
        </span>
        <Select
          value={view.filters.action === '' ? EMPTY_VALUE : view.filters.action}
          onValueChange={(v) => view.setFilter('action', v === EMPTY_VALUE ? '' : v)}
        >
          {/* Both ids, as `FilterSelect` does: a bare `aria-label` would replace the trigger's
              content as the accessible name and the chosen action would stop being announced. */}
          <SelectTrigger
            id={ACTION_TRIGGER_ID}
            aria-labelledby={`${ACTION_LABEL_ID} ${ACTION_TRIGGER_ID}`}
            className="w-full sm:w-56"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value={EMPTY_VALUE}>All actions</SelectItem>
            </SelectGroup>
            {actionGroups.map(([domain, codes]) => (
              <SelectGroup key={domain}>
                <SelectLabel>{domain}</SelectLabel>
                {codes.map((code) => (
                  <SelectItem key={code} value={code}>
                    {humanizeAction(code)}
                  </SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
        <DateTimeRange
          label="From"
          dateAriaLabel="From date"
          dateName="from-date"
          dateValue={view.filters.fromDate}
          onDateChange={(v) => view.setFilter('fromDate', v)}
          timeAriaLabel="From time"
          timeName="from-time"
          timeValue={view.filters.fromTime}
          onTimeChange={(v) => view.setFilter('fromTime', v)}
        />
        <DateTimeRange
          label="To"
          dateAriaLabel="To date"
          dateName="to-date"
          dateValue={view.filters.toDate}
          onDateChange={(v) => view.setFilter('toDate', v)}
          timeAriaLabel="To time"
          timeName="to-time"
          timeValue={view.filters.toTime}
          onTimeChange={(v) => view.setFilter('toTime', v)}
        />
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="audit-logs" view={view} columns={columns} />
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(r) => r.id}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        renderExpanded={renderExpanded}
        hiddenColumns={view.hiddenColumns}
        sort={{ by: view.orderBy, onChange: view.setOrderBy }}
        emptyLabel="No audit entries match these filters."
        emptyHint="Widen the date range or clear the action filter."
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
