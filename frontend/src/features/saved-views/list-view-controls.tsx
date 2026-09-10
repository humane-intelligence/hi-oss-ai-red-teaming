import { RotateCcw } from 'lucide-react'
import { usePermissions } from '@/lib/auth/use-permissions'
import { ColumnsMenu } from './columns-menu'
import { SavedViewsMenu } from './saved-views-menu'
import type { ListViewState } from './use-list-view-state'
import { Button } from '@/components/ui/button'
import type { Column } from '@/components/shared/data-table'
import type { SavedViewResource } from '@/lib/api/types'

// Clears the filters, so it belongs at the end of the filter group rather than with the view menus.
export function ResetViewButton<F extends Record<string, unknown>>({
  view,
}: {
  view: ListViewState<F>
}) {
  return (
    <Button variant="ghost" onClick={view.reset} disabled={!view.isDirty} aria-label="Reset view">
      <RotateCcw className="size-4" /> Reset
    </Button>
  )
}

// Toolbar cluster dropped into each list page: the saved-views menu (gated on
// `saved_views:read`) plus a column-visibility menu derived from the table's
// columns. Only columns with a stable `id` are toggleable.
export function ListViewControls<T, F extends Record<string, unknown>>({
  resource,
  view,
  columns,
}: {
  resource: SavedViewResource
  view: ListViewState<F>
  columns: Column<T>[]
}) {
  const { has } = usePermissions()
  const toggleable = columns
    .filter((col) => col.id)
    .map((col) => ({
      id: col.id as string,
      label: col.label ?? (typeof col.header === 'string' ? col.header : (col.id as string)),
      // Carried through so the menu can hide a toggle for a column CSS will not render anyway.
      hideBelow: col.hideBelow,
    }))

  return (
    // Wraps: upstream's Button base is `shrink-0`, so this cluster overflowed at 320px.
    <div className="flex flex-wrap gap-2">
      {has('saved_views:read') && <SavedViewsMenu resource={resource} view={view} />}
      {toggleable.length > 0 && (
        <ColumnsMenu
          columns={toggleable}
          hiddenColumns={view.hiddenColumns}
          onChange={view.setHiddenColumns}
        />
      )}
    </div>
  )
}
