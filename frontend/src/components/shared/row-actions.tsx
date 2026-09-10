import { useRef, type ComponentType } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

export type RowAction = {
  key: string
  // Menu text and the inline button's tooltip.
  label: string
  // Longer name for the icon-only button, which has no visible text.
  ariaLabel?: string
  icon: ComponentType<{ className?: string }>
  onSelect: () => void
  disabled?: boolean
  destructive?: boolean
  when?: boolean
}

// Icon buttons from `lg`, one menu below it. Five of them made the widest column in the app, and at
// 768px they return alongside the `md` columns, so the switch waits for `lg`.
export function RowActions({
  actions,
  menuLabel = 'Row actions',
}: {
  actions: RowAction[]
  menuLabel?: string
}) {
  const shown = actions.filter((a) => a.when !== false)
  const pendingAction = useRef<(() => void) | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  if (shown.length === 0) return null

  return (
    <div className="flex justify-end gap-1">
      {shown.map(({ key, label, ariaLabel, icon: Icon, onSelect, disabled }) => (
        <Button
          key={key}
          size="icon-sm"
          variant="outline"
          aria-label={ariaLabel ?? label}
          title={label}
          disabled={disabled}
          // The row itself navigates, so every control in it has to stop the click.
          onClick={(e) => {
            e.stopPropagation()
            onSelect()
          }}
          className="hidden lg:inline-flex"
        >
          <Icon />
        </Button>
      ))}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            ref={triggerRef}
            variant="ghost"
            size="icon-sm"
            aria-label={menuLabel}
            className="lg:hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          onCloseAutoFocus={(event) => {
            const action = pendingAction.current
            pendingAction.current = null
            if (!action) return
            // Radix's own restore would land after the action, so a `showModal()` that took focus
            // would lose it back to the trigger. Do the restore first instead, then act: a dialog
            // moves focus off the trigger, and a bare mutation leaves it there rather than on
            // `<body>` with nothing focused.
            event.preventDefault()
            triggerRef.current?.focus()
            action()
          }}
        >
          <DropdownMenuGroup>
            {shown.map(({ key, label, icon: Icon, onSelect, disabled, destructive }) => (
              <DropdownMenuItem
                key={key}
                disabled={disabled}
                variant={destructive ? 'destructive' : 'default'}
                // React replays events through the React tree, so a portalled item still reaches
                // the row's `onClick` and would navigate away from the dialog it just opened.
                onClick={(e) => e.stopPropagation()}
                onSelect={() => {
                  pendingAction.current = onSelect
                }}
              >
                <Icon /> {label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
