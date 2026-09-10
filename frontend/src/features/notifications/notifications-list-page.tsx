import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCheck, Mail, MailOpen } from 'lucide-react'
import { useNotifications } from './queries'
import { useMarkNotifications } from './mutations'
import { objectHref } from './object-link'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { NotificationResponse } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 20

export function NotificationsListPage() {
  const { has } = usePermissions()
  const canMark = has('notifications:update')
  const [offset, setOffset] = useState(0)
  const [unreadOnly, setUnreadOnly] = useState(false)
  const mark = useMarkNotifications()
  const query = useNotifications({ limit: PAGE_SIZE, offset, read: unreadOnly ? false : undefined })
  const page = query.data

  const columns: Column<NotificationResponse>[] = [
    {
      id: 'status',
      header: '',
      className: 'w-6',
      cell: (n) =>
        n.read ? null : (
          <span className="bg-primary inline-block size-2 rounded-full" aria-label="unread" />
        ),
    },
    {
      id: 'name',
      header: 'Name',
      // Carries the description too: its column waits for `lg` and there is no detail view.
      cell: (n) => (
        <span
          title={n.description ?? undefined}
          className={cn('line-clamp-2', !n.read && 'font-medium')}
        >
          {n.name}
        </span>
      ),
    },
    {
      id: 'description',
      header: 'Description',
      // Not `sm`: this column alone wanted 205px, putting the table 198px over its card at 640.
      hideBelow: 'lg',
      cell: (n) => (
        <span className="text-muted-foreground line-clamp-2 max-w-md">{n.description ?? '—'}</span>
      ),
    },
    {
      id: 'object',
      // Never dropped: the row's only link out, and this table has no detail route or expander.
      header: 'Object',
      cell: (n) => {
        if (!n.object_type) return null
        const badge = (
          <Badge variant="neutral" className="max-w-[6rem] truncate sm:max-w-none">
            {n.object_type}
          </Badge>
        )
        const href = objectHref(n)
        return href ? (
          <Link to={href} className="hover:underline">
            {badge}
          </Link>
        ) : (
          badge
        )
      },
    },
    {
      id: 'created',
      header: 'Received',
      hideBelow: 'lg',
      cell: (n) => {
        const at = new Date(n.created_at)
        return (
          <span className="text-muted-foreground whitespace-nowrap">
            <span className="xl:hidden">{at.toLocaleDateString()}</span>
            <span className="hidden xl:inline">{at.toLocaleString()}</span>
          </span>
        )
      },
    },
    {
      id: 'actions',
      header: '',
      className: 'text-right',
      cell: (n) =>
        canMark ? (
          <Button
            variant="ghost"
            size="sm"
            aria-label={n.read ? 'Mark unread' : 'Mark read'}
            title={n.read ? 'Mark unread' : 'Mark read'}
            onClick={() => mark.mutate({ ids: [n.id], read: !n.read })}
            disabled={mark.isPending}
          >
            {n.read ? <Mail /> : <MailOpen />}
            {/* The widest thing in the row, and `lg` already spends its width on two columns. */}
            <span className="hidden xl:inline">{n.read ? 'Mark unread' : 'Mark read'}</span>
          </Button>
        ) : null,
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="Notifications"
        description="Your notifications."
        actions={
          <div className="flex w-full gap-2 sm:max-w-sm sm:min-w-64 sm:flex-1">
            <Button
              variant={unreadOnly ? 'outline' : 'default'}
              size="sm"
              aria-pressed={!unreadOnly}
              onClick={() => {
                setUnreadOnly(false)
                setOffset(0)
              }}
            >
              All
            </Button>
            <Button
              variant={unreadOnly ? 'default' : 'outline'}
              size="sm"
              aria-pressed={unreadOnly}
              onClick={() => {
                setUnreadOnly(true)
                setOffset(0)
              }}
            >
              Unread
            </Button>
            {canMark && (
              <Button
                variant="outline"
                onClick={() => mark.mutate({ ids: [], read: true })}
                disabled={mark.isPending}
              >
                <CheckCheck className="size-4" /> Mark all read
              </Button>
            )}
          </div>
        }
      />
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(n) => n.id}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        emptyLabel={unreadOnly ? "You're all caught up." : 'No notifications yet.'}
      />
      {page && (
        <Pagination
          offset={offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={setOffset}
        />
      )}
    </div>
  )
}
