import { type KeyboardEvent, type ReactNode, Fragment, useState } from 'react'
import { ChevronDown, ChevronRight, ChevronUp, Inbox } from 'lucide-react'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { humanizeError } from '@/lib/api/problem'

// Tailwind cannot take a computed class name, so the breakpoints are spelled out.
const HIDE_BELOW = {
  sm: 'hidden sm:table-cell',
  md: 'hidden md:table-cell',
  lg: 'hidden lg:table-cell',
  xl: 'hidden xl:table-cell',
} as const

export type HideBelow = keyof typeof HIDE_BELOW

export type Column<T> = {
  header: ReactNode
  cell: (row: T) => ReactNode
  className?: string
  // Drops the column below this breakpoint, so a wide table needs no sideways scroll on a phone.
  // The cell stays in the DOM; the row still opens a detail view carrying every field.
  hideBelow?: HideBelow
  sortKey?: string
  // Stable id + human label drive column-visibility toggling (ColumnsMenu / saved views).
  // A column without an `id` can never be hidden.
  id?: string
  label?: string
}

type SortState = {
  by: string | undefined
  onChange: (next: string) => void
}

type DataTableProps<T> = {
  columns: Column<T>[]
  rows: T[] | undefined
  rowKey: (row: T) => string
  onRowClick?: (row: T) => void
  isLoading?: boolean
  isError?: boolean
  error?: unknown
  emptyLabel?: string
  emptyHint?: ReactNode
  emptyAction?: ReactNode
  sort?: SortState
  // Ids of columns to hide (from a ColumnsMenu / saved view). Columns without an `id` are never hidden.
  hiddenColumns?: string[]
  // When provided, each row gets a leading expander toggle; returning non-null
  // reveals that content in a full-width row below. Independent of `onRowClick`
  // (the chevron stops propagation), so a row can both navigate and expand.
  renderExpanded?: (row: T) => ReactNode
  // When provided, a leading checkbox column selects rows. `selectedKeys` is owned by the
  // caller; `rowSelectable` (default: all) gates which rows can be picked and which the
  // header "select all" spans. The checkbox stops propagation so it never triggers `onRowClick`.
  selection?: {
    selectedKeys: Set<string>
    onSelectedChange: (next: Set<string>) => void
    rowSelectable?: (row: T) => boolean
  }
}

const colClass = <T,>(col: Column<T>) =>
  cn(col.className, col.hideBelow && HIDE_BELOW[col.hideBelow])

function SortableHead({
  children,
  sortKey,
  sort,
  className,
}: {
  children: ReactNode
  sortKey: string
  sort: SortState
  className?: string
}) {
  const isAsc = sort.by === sortKey
  const isDesc = sort.by === `-${sortKey}`
  const active = isAsc || isDesc

  function handleClick() {
    // toggle: unsorted → asc → desc → asc …
    if (!active || isDesc) {
      sort.onChange(sortKey)
    } else {
      sort.onChange(`-${sortKey}`)
    }
  }

  return (
    <TableHead
      className={className}
      aria-sort={isAsc ? 'ascending' : isDesc ? 'descending' : 'none'}
    >
      <button
        type="button"
        onClick={handleClick}
        className={cn(
          'hover:text-foreground flex items-center gap-1 whitespace-nowrap',
          active ? 'text-foreground' : 'text-muted-foreground',
        )}
      >
        {children}
        {isAsc ? (
          <ChevronUp className="size-3.5" />
        ) : isDesc ? (
          <ChevronDown className="size-3.5" />
        ) : (
          <ChevronUp className="size-3.5 opacity-30" />
        )}
      </button>
    </TableHead>
  )
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isLoading,
  isError,
  error,
  emptyLabel = 'Nothing here yet.',
  emptyHint,
  emptyAction,
  sort,
  hiddenColumns,
  renderExpanded,
  selection,
}: DataTableProps<T>) {
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set())
  const expandable = !!renderExpanded
  const visibleColumns = hiddenColumns?.length
    ? columns.filter((col) => !(col.id && hiddenColumns.includes(col.id)))
    : columns
  const span = visibleColumns.length + (expandable ? 1 : 0) + (selection ? 1 : 0)

  const isRowSelectable = (row: T) =>
    selection?.rowSelectable ? selection.rowSelectable(row) : true
  const selectableRows = (rows ?? []).filter(isRowSelectable)
  const allSelected =
    selectableRows.length > 0 &&
    selectableRows.every((row) => selection?.selectedKeys.has(rowKey(row)))

  function toggleSelected(key: string) {
    if (!selection) return
    const next = new Set(selection.selectedKeys)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    selection.onSelectedChange(next)
  }

  function toggleSelectAll() {
    if (!selection) return
    const next = new Set(selection.selectedKeys)
    for (const row of selectableRows) {
      const key = rowKey(row)
      if (allSelected) next.delete(key)
      else next.add(key)
    }
    selection.onSelectedChange(next)
  }

  // Prune expansion state for rows that are gone (refetch / paging), so the set can't
  // grow unbounded and a later-reused key can't render pre-expanded. Reconciled during
  // render (React's "adjust state on prop change" pattern) rather than in an effect.
  // `'\0'` as a source escape, not a literal NUL: a raw NUL byte made git treat this file as
  // binary, so none of its changes were reviewable in a diff. A key cannot contain one, so the
  // signature still cannot collide.
  const rowKeySignature = (rows ?? []).map(rowKey).join('\0')
  const [seenSignature, setSeenSignature] = useState(rowKeySignature)
  if (rowKeySignature !== seenSignature) {
    setSeenSignature(rowKeySignature)
    setExpandedKeys((prev) => {
      if (prev.size === 0) return prev
      const valid = new Set((rows ?? []).map(rowKey))
      const next = new Set([...prev].filter((key) => valid.has(key)))
      return next.size === prev.size ? prev : next
    })
  }

  function toggle(key: string) {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className="bg-card rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            {selection && (
              <TableHead className="w-8">
                <input
                  type="checkbox"
                  aria-label="Select all rows"
                  checked={allSelected}
                  onChange={toggleSelectAll}
                />
              </TableHead>
            )}
            {expandable && <TableHead className="w-8" aria-hidden />}
            {visibleColumns.map((col, i) =>
              col.sortKey && sort ? (
                <SortableHead key={i} sortKey={col.sortKey} sort={sort} className={colClass(col)}>
                  {col.header}
                </SortableHead>
              ) : (
                <TableHead key={i} className={colClass(col)}>
                  {col.header}
                </TableHead>
              ),
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {isError ? (
            <TableRow>
              <TableCell colSpan={span} className="text-destructive">
                {humanizeError(error)}
              </TableCell>
            </TableRow>
          ) : isLoading ? (
            Array.from({ length: 5 }).map((_, r) => (
              <TableRow key={r} data-testid="datatable-skeleton">
                {selection && <TableCell className="w-8" />}
                {expandable && <TableCell className="w-8" />}
                {visibleColumns.map((col, c) => (
                  <TableCell key={c} className={colClass(col)}>
                    <div className="bg-muted h-4 w-full animate-pulse rounded" />
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : !rows || rows.length === 0 ? (
            <TableRow>
              <TableCell colSpan={span}>
                <div className="flex flex-col items-center gap-3 py-6">
                  <Inbox className="text-muted-foreground/40 size-8" />
                  <span className="text-muted-foreground">{emptyLabel}</span>
                  {emptyHint && (
                    <span className="text-muted-foreground/70 max-w-sm text-center text-sm">
                      {emptyHint}
                    </span>
                  )}
                  {emptyAction}
                </div>
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => {
              const key = rowKey(row)
              const expandedContent = renderExpanded?.(row)
              const isExpanded = expandedKeys.has(key)
              return (
                <Fragment key={key}>
                  <TableRow
                    className={cn(onRowClick && 'cursor-pointer')}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    tabIndex={onRowClick ? 0 : undefined}
                    onKeyDown={
                      onRowClick
                        ? (e: KeyboardEvent) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              onRowClick(row)
                            }
                          }
                        : undefined
                    }
                  >
                    {selection && (
                      <TableCell className="w-8">
                        <input
                          type="checkbox"
                          aria-label="Select row"
                          checked={selection.selectedKeys.has(key)}
                          disabled={!isRowSelectable(row)}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => toggleSelected(key)}
                        />
                      </TableCell>
                    )}
                    {expandable && (
                      <TableCell className="w-8">
                        {expandedContent != null && (
                          <button
                            type="button"
                            aria-label={isExpanded ? 'Collapse row' : 'Expand row'}
                            aria-expanded={isExpanded}
                            className="text-muted-foreground hover:text-foreground flex items-center rounded"
                            onClick={(e) => {
                              e.stopPropagation()
                              toggle(key)
                            }}
                            onKeyDown={(e) => {
                              // Don't let Enter/Space bubble to the row's navigate handler.
                              if (e.key === 'Enter' || e.key === ' ') e.stopPropagation()
                            }}
                          >
                            {isExpanded ? (
                              <ChevronDown className="size-4" />
                            ) : (
                              <ChevronRight className="size-4" />
                            )}
                          </button>
                        )}
                      </TableCell>
                    )}
                    {visibleColumns.map((col, i) => (
                      <TableCell key={i} className={colClass(col)}>
                        {col.cell(row)}
                      </TableCell>
                    ))}
                  </TableRow>
                  {expandedContent != null && isExpanded && (
                    <TableRow>
                      <TableCell colSpan={span} className="bg-muted/30 p-4">
                        {expandedContent}
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              )
            })
          )}
        </TableBody>
      </Table>
    </div>
  )
}
