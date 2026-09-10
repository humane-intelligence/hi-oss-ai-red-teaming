import { useState } from 'react'
import { Bell } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useNotifications, useUnreadCount } from './queries'
import { useMarkNotifications } from './mutations'
import { objectHref } from './object-link'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dropdown } from '@/components/ui/dropdown'
import { usePermissions } from '@/lib/auth/use-permissions'
import { cn } from '@/lib/utils'

const PREVIEW_LIMIT = 8

export function NotificationBell() {
  const { has } = usePermissions()
  const canMark = has('notifications:update')
  const [open, setOpen] = useState(false)
  const unread = useUnreadCount()
  // Fetch the preview only while the panel is open, and always fresh — no wasted request per app
  // load, and no stale panel when the badge has advanced since it was last opened.
  const list = useNotifications(
    { limit: PREVIEW_LIMIT, offset: 0 },
    { enabled: open, staleTime: 0 },
  )
  const mark = useMarkNotifications()
  const count = unread.data ?? 0
  const items = list.data?.items ?? []

  const label = (
    <>
      <Bell className="size-4" />
      {count > 0 && (
        <Badge
          className="absolute -top-1 -right-1 min-w-4 justify-center rounded-full px-1 py-0 text-[10px] leading-4"
          aria-hidden
        >
          {count > 9 ? '9+' : count}
        </Badge>
      )}
    </>
  )

  return (
    <Dropdown
      label={label}
      ariaLabel={count > 0 ? `Notifications, ${count} unread` : 'Notifications'}
      triggerVariant="ghost"
      triggerSize="icon"
      triggerClassName="relative"
      panelClassName="w-80 p-0"
      onOpenChange={setOpen}
    >
      {(close) => (
        <div className="flex flex-col">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-sm font-medium">Notifications</span>
            {count > 0 && canMark && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => mark.mutate({ ids: [], read: true })}
                disabled={mark.isPending}
              >
                Mark all read
              </Button>
            )}
          </div>
          {items.length === 0 ? (
            <p className="text-muted-foreground px-3 py-6 text-center text-sm">
              {list.isPending ? 'Loading…' : "You're all caught up"}
            </p>
          ) : (
            <ul className="max-h-80 divide-y overflow-y-auto">
              {items.map((n) => (
                <li key={n.id}>
                  <Link
                    to={objectHref(n) ?? '/notifications'}
                    onClick={close}
                    className="hover:bg-accent flex gap-2 px-3 py-2"
                  >
                    <span
                      className={cn(
                        'mt-1.5 size-2 shrink-0 rounded-full',
                        n.read ? 'bg-transparent' : 'bg-primary',
                      )}
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1">
                      {!n.read && <span className="sr-only">Unread: </span>}
                      <span className={cn('block truncate text-sm', !n.read && 'font-medium')}>
                        {n.name}
                      </span>
                      <span className="text-muted-foreground block text-xs">
                        {new Date(n.created_at).toLocaleString()}
                      </span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <Link
            to="/notifications"
            onClick={close}
            className="text-primary block border-t px-3 py-2 text-center text-sm hover:underline"
          >
            See all
          </Link>
        </div>
      )}
    </Dropdown>
  )
}
