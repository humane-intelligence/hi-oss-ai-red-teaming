// The caller loads the roles: the whole catalog for a global assignment, or what the group
// accepts (`useGroupAssignableRoles`) for an in-group one.
export function RolesPicker({
  roles,
  isPending,
  isError = false,
  selected,
  onChange,
  describedBy,
  invalid,
}: {
  roles: { id: string; display_name: string }[]
  isPending: boolean
  isError?: boolean
  selected: string[]
  onChange: (ids: string[]) => void
  // A checkbox group carries its description and validity on the group, not on each box, so these
  // land on the wrapper. `FormField` cannot clone them onto this component - it renders its own
  // prop list and would swallow them.
  describedBy?: string
  invalid?: boolean
}) {
  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])

  if (isPending) return <p className="text-muted-foreground text-xs">Loading roles…</p>
  // Not the empty state: an unreadable list says nothing about which roles exist.
  if (isError) return <p className="text-destructive text-xs">Couldn’t load the roles to assign.</p>

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
