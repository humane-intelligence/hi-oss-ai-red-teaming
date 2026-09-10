import { SlidersHorizontal } from 'lucide-react'
import { Dropdown } from '@/components/ui/dropdown'
import type { HideBelow } from '@/components/shared/data-table'
import { cn } from '@/lib/utils'

export type ToggleableColumn = { id: string; label: string; hideBelow?: HideBelow }

// The table's own `hideBelow` classes, as a flex row rather than a table cell. Same breakpoints, so
// a toggle disappears exactly where the column it controls cannot render - offering it there would
// let a reader tick a box and see nothing happen.
const HIDE_ROW_BELOW: Record<HideBelow, string> = {
  sm: 'hidden sm:flex',
  md: 'hidden md:flex',
  lg: 'hidden lg:flex',
  xl: 'hidden xl:flex',
}

// Checkbox list of the toggleable columns. `hiddenColumns` holds the ids that
// are OFF; a checked box means visible.
export function ColumnsMenu({
  columns,
  hiddenColumns,
  onChange,
}: {
  columns: ToggleableColumn[]
  hiddenColumns: string[]
  onChange: (hidden: string[]) => void
}) {
  const toggle = (id: string) =>
    onChange(
      hiddenColumns.includes(id) ? hiddenColumns.filter((x) => x !== id) : [...hiddenColumns, id],
    )

  return (
    <Dropdown
      ariaLabel="Toggle columns"
      label={
        <>
          <SlidersHorizontal className="size-4" /> Columns
        </>
      }
    >
      {() => (
        <>
          {columns.map((col) => (
            <label
              key={col.id}
              className={cn(
                'hover:bg-accent flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm select-none',
                col.hideBelow && HIDE_ROW_BELOW[col.hideBelow],
              )}
            >
              <input
                type="checkbox"
                className="size-4 rounded border"
                checked={!hiddenColumns.includes(col.id)}
                onChange={() => toggle(col.id)}
              />
              {col.label}
            </label>
          ))}
        </>
      )}
    </Dropdown>
  )
}
