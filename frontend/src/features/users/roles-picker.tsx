import { useGroupAssignableRoles } from '@/features/evaluation-groups/queries'
import { useRoles } from './queries'

export function RolesPicker({
  selected,
  onChange,
  groupId,
  describedBy,
  invalid,
}: {
  selected: string[]
  onChange: (ids: string[]) => void
  // A checkbox group carries its description and validity on the group, not on each box, so these
  // land on the wrapper. `FormField` cannot clone them onto this component - it renders its own
  // prop list and would swallow them.
  describedBy?: string
  invalid?: boolean
  // Set for an in-group picker: it then offers the roles assignable in that group, which the
  // backend lists to whoever manages its members. Omitted, it offers the whole catalog.
  groupId?: string
}) {
  const catalog = useRoles({ enabled: groupId === undefined })
  const inGroup = useGroupAssignableRoles(groupId ?? '')
  const { data, isPending } = groupId === undefined ? catalog : inGroup
  const roles = data?.items ?? []

  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])

  if (isPending) return <p className="text-muted-foreground text-xs">Loading roles…</p>

  return (
    <div
      role="group"
      aria-describedby={describedBy}
      aria-invalid={invalid ? true : undefined}
      className="space-y-1.5"
    >
      {roles.length === 0 && (
        <p className="text-muted-foreground text-xs">No roles available to pick.</p>
      )}
      {roles.map((r) => (
        <label key={r.id} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="size-4 rounded border"
            checked={selected.includes(r.id)}
            onChange={() => toggle(r.id)}
          />
          {r.display_name}
        </label>
      ))}
    </div>
  )
}
