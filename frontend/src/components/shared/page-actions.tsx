import type { ComponentType, ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

export type PageAction = {
  key: string
  label: string
  icon?: ComponentType<{ className?: string }>
  onSelect: () => void
  disabled?: boolean
  destructive?: boolean
  // Inline buttons only: a long-press tooltip is not reachable on a phone.
  title?: string
  // Callers pass their permission check here rather than building the array conditionally.
  when?: boolean
}

// `secondary` renders inline from `sm` and as a menu below; a full set of these overflows a phone.
// `primary` is JSX because some pages pass a status-dependent cluster with its own disabled reasons.
export function PageActions({
  primary,
  secondary = [],
  className,
  menuLabel = 'More actions',
}: {
  primary?: ReactNode
  secondary?: PageAction[]
  className?: string
  menuLabel?: string
}) {
  const shown = secondary.filter((a) => a.when !== false)

  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      {primary}

      {shown.map(({ key, label, icon: Icon, onSelect, disabled, destructive, title }) => (
        <Button
          key={key}
          variant="outline"
          size="sm"
          disabled={disabled}
          title={title}
          onClick={onSelect}
          className={cn(
            'hidden sm:inline-flex',
            // Red text on an outline button, the way the menu item reads it. The filled
            // `destructive` variant would outweigh the primary CTA beside it.
            destructive &&
              'text-destructive hover:text-destructive hover:bg-destructive/10 [&_svg]:text-destructive',
          )}
        >
          {Icon && <Icon />} {label}
        </Button>
      ))}

      {shown.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon-sm" aria-label={menuLabel} className="sm:hidden">
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuGroup>
              {shown.map(({ key, label, icon: Icon, onSelect, disabled, destructive }) => (
                <DropdownMenuItem
                  key={key}
                  disabled={disabled}
                  onSelect={onSelect}
                  variant={destructive ? 'destructive' : 'default'}
                >
                  {Icon && <Icon />} {label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}

// Title block beside its actions from `sm`, stacked below.
export function DetailHeader({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn('flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between', className)}
    >
      {children}
    </div>
  )
}
