import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { useRolesAdmin } from './queries'
import { useRestoreRole } from './mutations'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { Pagination } from '@/components/shared/pagination'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { RoleResponse } from '@/lib/api/types'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

const PAGE_SIZE = 20

const columns: Column<RoleResponse>[] = [
  {
    id: 'role',
    header: 'Role',
    cell: (r) => (
      <div>
        <div className="font-medium">{r.display_name}</div>
        <div className="text-muted-foreground font-mono text-xs">{r.name}</div>
      </div>
    ),
  },
  {
    id: 'kind',
    header: 'Kind',
    hideBelow: 'md',
    cell: (r) => (
      <Badge variant={r.is_system ? 'secondary' : 'outline'}>
        {r.is_system ? 'System' : 'Custom'}
      </Badge>
    ),
  },
  {
    id: 'status',
    header: 'Status',
    cell: (r) => (
      <div className="flex flex-wrap gap-1">
        <Badge variant={r.is_active ? 'default' : 'secondary'}>
          {r.is_active ? 'Active' : 'Inactive'}
        </Badge>
        {r.is_default && <Badge variant="outline">Default</Badge>}
        {r.is_participant_default && <Badge variant="outline">Self-join default</Badge>}
        {!r.is_system && r.is_object_assignable && <Badge variant="outline">In-group</Badge>}
      </div>
    ),
  },
  {
    id: 'permissions',
    header: 'Permissions',
    cell: (r) => {
      const perms = r.permissions ?? []
      if (perms.length === 0) return <span className="text-muted-foreground">0</span>
      return (
        // `role="img"` so the aria-label is exposed: the list is otherwise hover-only, and
        // this cell is the only place a roles:read operator can see what a role grants.
        <span
          role="img"
          className="cursor-help underline decoration-dotted underline-offset-2"
          title={perms.join('\n')}
          aria-label={`${perms.length} permissions: ${perms.join(', ')}`}
        >
          {perms.length}
        </span>
      )
    },
  },
]

export function RolesListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const [offset, setOffset] = useState(0)
  const [includeInactive, setIncludeInactive] = useState(false)
  const [viewingDeleted, setViewingDeleted] = useState(false)
  const canManage = has('roles:manage')
  // The whole mode is gated, not just the toggle: a caller who loses `roles:manage`
  // mid-session must not keep querying a view the server will 403.
  const deleted = viewingDeleted && canManage
  const query = useRolesAdmin({ limit: PAGE_SIZE, offset, includeInactive, deleted })
  const page = query.data
  const restore = useRestoreRole()

  const deletedColumns: Column<RoleResponse>[] = [
    // Action first: on a tombstone row it is the only affordance (there is no detail page to
    // open), and a trailing column is the first thing a narrow viewport puts behind a scroll.
    {
      id: 'restore',
      header: '',
      cell: (r) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore role: ${r.display_name}`}
          disabled={restore.isPending && restore.variables === r.id}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(r.id)
          }}
        >
          Restore
        </Button>
      ),
    },
    ...columns,
    {
      id: 'deleted_at',
      label: 'Deleted',
      header: 'Deleted',
      cell: (r) =>
        r.deleted_at ? (
          <time dateTime={r.deleted_at} className="text-muted-foreground text-xs">
            {new Date(r.deleted_at).toLocaleString()}
          </time>
        ) : null,
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="Roles"
        description="Platform and custom roles, and the permissions each grants."
      />
      <div className="flex flex-wrap items-center gap-3">
        {!deleted && (
          <Label className="text-muted-foreground text-sm font-normal">
            <Checkbox
              checked={includeInactive}
              onCheckedChange={(next) => {
                setIncludeInactive(next === true)
                setOffset(0)
              }}
            />
            Show inactive
          </Label>
        )}
        {canManage && (
          <DeletedToggle
            value={viewingDeleted}
            onChange={(next) => {
              setViewingDeleted(next)
              setOffset(0)
            }}
            label="Which roles to show"
          />
        )}
        {canManage && !deleted && (
          <Button className="sm:ml-auto" onClick={() => navigate('/roles/new')}>
            <Plus className="size-4" /> New role
          </Button>
        )}
      </div>
      <DataTable
        columns={deleted ? deletedColumns : columns}
        rows={page?.items}
        rowKey={(r) => r.id}
        // A tombstone has no edit page — the row action is the only thing that reaches it.
        onRowClick={canManage && !deleted ? (r) => navigate(`/roles/${r.id}/edit`) : undefined}
        isLoading={query.isPending || query.isPlaceholderData}
        isError={query.isError}
        error={query.error}
        emptyLabel={deleted ? 'Nothing deleted recently.' : 'No roles yet.'}
        emptyHint={
          deleted
            ? 'Deleted roles stay here for a limited time, then stop being restorable.'
            : canManage
              ? 'Create a custom role to grant a tailored permission set.'
              : undefined
        }
        emptyAction={
          canManage && !deleted ? (
            <Button onClick={() => navigate('/roles/new')}>
              <Plus className="size-4" /> New role
            </Button>
          ) : undefined
        }
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
