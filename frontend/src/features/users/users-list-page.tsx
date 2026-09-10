import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { KeyRound, LogOut, Mail, MailX, RotateCw, UserCheck, UserX } from 'lucide-react'
import { useUsers, useRoles, type UserOrderBy } from './queries'
import {
  useBulkChangeUserStatus,
  useBulkForceLogout,
  useBulkSendPasswordReset,
  useChangeUserStatus,
  useForceLogout,
  useResendInvitation,
  useRestoreUser,
  useRevokeInvitation,
  useSendPasswordReset,
} from './mutations'
import { BulkResultDialog, type BulkOutcome } from './bulk-result-dialog'
import { UserStatusBadge } from './status-badge'
import { InviteDialog } from './invite-dialog'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { RowActions } from '@/components/shared/row-actions'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { StatusPill } from '@/components/shared/status-pill'
import { DataTable, type Column } from '@/components/shared/data-table'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { Pagination } from '@/components/shared/pagination'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { FilterSelect } from '@/components/shared/filter-select'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useAuth } from '@/lib/auth/auth-context'
import type { UserResponse, UserStatus } from '@/lib/api/types'

const USER_STATUSES: UserStatus[] = ['active', 'pending', 'invited', 'inactive']

const PAGE_SIZE = 20

type UserFilters = { status: UserStatus | ''; roleId: string; deleted: boolean }
const DEFAULT_FILTERS: UserFilters = { status: '', roleId: '', deleted: false }
const DEFAULT_ORDER_BY = '-created_at'

type BulkAction = 'logout' | 'activate' | 'deactivate' | 'reset'

function bulkSpec(action: BulkAction, count: number) {
  const users = `${count} selected user${count === 1 ? '' : 's'}`
  switch (action) {
    case 'logout':
      return {
        title: 'Force logout users',
        confirmLabel: 'Force logout',
        destructive: true,
        description: `Sign out ${users}? All their active sessions are revoked immediately.`,
      }
    case 'deactivate':
      return {
        title: 'Deactivate accounts',
        confirmLabel: 'Deactivate',
        destructive: true,
        description: `Deactivate ${users}? They are signed out immediately and cannot sign in until reactivated. Accounts still onboarding are reported as failed.`,
      }
    case 'activate':
      return {
        title: 'Activate accounts',
        confirmLabel: 'Activate',
        destructive: false,
        description: `Activate ${users}? Accounts still onboarding are reported as failed.`,
      }
    case 'reset':
      return {
        title: 'Send password reset links',
        confirmLabel: 'Send links',
        destructive: false,
        description: `Mail a password reset link to ${users}? Only active accounts with a password of their own can be reset; the rest are reported as failed.`,
      }
  }
}

function fullName(u: UserResponse) {
  const name = [u.first_name, u.last_name].filter(Boolean).join(' ')
  return name || '—'
}

const columns: Column<UserResponse>[] = [
  {
    id: 'email',
    label: 'Email',
    header: 'Email',
    // Untruncated an address took 230px of a 286px table at 320px.
    cell: (u) => (
      <span className="block max-w-[7rem] truncate font-medium sm:max-w-[13rem]">{u.email}</span>
    ),
    sortKey: 'email',
  },
  { id: 'name', label: 'Name', header: 'Name', hideBelow: 'lg', cell: (u) => fullName(u) },
  {
    id: 'organization',
    label: 'Organization',
    header: 'Organization',
    hideBelow: 'lg',
    cell: (u) =>
      u.organization ? u.organization.name : <span className="text-muted-foreground">—</span>,
  },
  {
    id: 'status',
    label: 'Status',
    header: 'Status',
    // Kept at every width: the row's only destination is the edit form, which omits the status.
    cell: (u) => <UserStatusBadge status={u.status} compact />,
    sortKey: 'status',
  },
  {
    // No sortKey/filter: the backend's users order_by enum has no invitation key.
    id: 'invitation',
    label: 'Invitation',
    header: 'Invitation',
    // Safe at `xl` only because the edit page carries the invitation and its expiry.
    hideBelow: 'xl',
    cell: (u) =>
      u.invitation ? (
        <div className="flex flex-col gap-0.5">
          <span>
            <StatusPill status={u.invitation.status} />
          </span>
          {u.invitation.status === 'pending' && (
            <span className="text-muted-foreground text-xs">
              expires {new Date(u.invitation.expires_at).toLocaleDateString()}
            </span>
          )}
        </div>
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    id: 'verified',
    label: 'Verified',
    header: 'Verified',
    // Also safe at `xl`: the edit page states it.
    hideBelow: 'xl',
    cell: (u) => (u.email_verified ? 'Yes' : 'No'),
  },
  {
    id: 'roles',
    label: 'Roles',
    header: 'Roles',
    hideBelow: 'xl',
    cell: (u) => (
      <div className="flex flex-wrap gap-1">
        {(u.roles ?? []).map((role) => (
          <Badge key={role.id} variant="secondary">
            {role.display_name}
          </Badge>
        ))}
      </div>
    ),
  },
]

export function UsersListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const { user: me } = useAuth()
  const [inviteOpen, setInviteOpen] = useState(false)
  const view = useListViewState<UserFilters>({
    defaultFilters: DEFAULT_FILTERS,
    defaultOrderBy: DEFAULT_ORDER_BY,
  })
  const roles = useRoles()
  // A saved view keeps `deleted` in its filter blob, so gate the whole mode rather than just
  // the toggle — a caller who has since lost `users:delete` would otherwise land on dead
  // Restore buttons with no control to leave.
  const viewingDeleted = view.filters.deleted && has('users:delete')
  const query = useUsers({
    limit: PAGE_SIZE,
    offset: view.offset,
    status: view.filters.status || undefined,
    email: view.search || undefined,
    role_id: view.filters.roleId || undefined,
    order_by: (view.orderBy ?? DEFAULT_ORDER_BY) as UserOrderBy,
    deleted: viewingDeleted,
  })
  const page = query.data
  const restore = useRestoreUser()

  const canUpdate = has('users:update')
  const canInvite = has('users:invite')
  const canManageSessions = has('users:manage_sessions')

  const forceLogout = useForceLogout()
  const bulkForceLogout = useBulkForceLogout()
  const resendInvitation = useResendInvitation()
  const revokeInvitation = useRevokeInvitation()
  const changeStatus = useChangeUserStatus()
  const sendPasswordReset = useSendPasswordReset()
  const bulkChangeStatus = useBulkChangeUserStatus()
  const bulkSendPasswordReset = useBulkSendPasswordReset()
  const [logoutTarget, setLogoutTarget] = useState<UserResponse | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<UserResponse | null>(null)
  const [deactivateTarget, setDeactivateTarget] = useState<UserResponse | null>(null)
  const [resetTarget, setResetTarget] = useState<UserResponse | null>(null)
  const [bulkAction, setBulkAction] = useState<BulkAction | null>(null)
  const [bulkResult, setBulkResult] = useState<{
    title: string
    result: BulkOutcome
    labels: Map<string, string>
  } | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set())

  // Selection is page-scoped: prune ids not in the current rows during render, which also
  // clears it on a page change (mirrors review-queue-page). Not an effect (eslint bans it).
  const rowIds = new Set((page?.items ?? []).map((u) => u.id))
  const rowSig = [...rowIds].join(' ')
  const [seenSig, setSeenSig] = useState(rowSig)
  if (rowSig !== seenSig) {
    setSeenSig(rowSig)
    setSelectedKeys((prev) => {
      if (prev.size === 0) return prev
      const next = new Set([...prev].filter((k) => rowIds.has(k)))
      return next.size === prev.size ? prev : next
    })
  }

  const bulkPending =
    bulkForceLogout.isPending || bulkChangeStatus.isPending || bulkSendPasswordReset.isPending
  const pendingBulk = bulkAction ? bulkSpec(bulkAction, selectedKeys.size) : null
  const selectionLabel = `${selectedKeys.size} user${selectedKeys.size === 1 ? '' : 's'} selected`

  function runBulk(action: BulkAction) {
    const ids = [...selectedKeys]
    const emailById = new Map((page?.items ?? []).map((u) => [u.id, u.email]))
    // Snapshot the labels: a status filter drops the touched rows from the page before the result
    // dialog renders, and a raw uuid in a failure list tells the admin nothing.
    const labels = new Map(ids.map((id) => [id, emailById.get(id) ?? id]))
    const rows = ids.map((id) => ({ row_key: id, data: { user_id: id } }))
    const finish = (title: string) => (result: BulkOutcome) => {
      setBulkAction(null)
      setSelectedKeys(new Set())
      // The toast already carries the counts; the per-row list only matters when a row failed.
      if (result.failed > 0) setBulkResult({ title, result, labels })
    }
    if (action === 'logout') {
      bulkForceLogout.mutate(
        { dry_run: false, rows },
        { onSuccess: finish('Force logout results') },
      )
    } else if (action === 'reset') {
      bulkSendPasswordReset.mutate(
        { dry_run: false, rows },
        { onSuccess: finish('Password reset results') },
      )
    } else {
      const status = action === 'activate' ? 'active' : 'inactive'
      bulkChangeStatus.mutate(
        { dry_run: false, rows: rows.map((r) => ({ ...r, data: { ...r.data, status } })) },
        {
          onSuccess: finish(action === 'activate' ? 'Activation results' : 'Deactivation results'),
        },
      )
    }
  }

  // A tombstoned account takes none of the live row actions (no logout, no status flip, no
  // invitation) — the one thing that reaches it is the restore.
  const deletedColumns: Column<UserResponse>[] = [
    // Action first: on a tombstone row it is the only affordance (there is no detail page to
    // open), and a trailing column is the first thing a narrow viewport puts behind a scroll.
    {
      id: 'restore',
      header: '',
      cell: (u) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore user: ${u.email}`}
          disabled={restore.isPending && restore.variables === u.id}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(u.id)
          }}
        >
          Restore
        </Button>
      ),
    },
    // The live `status` column goes, as on the models table: a tombstone rendering an
    // ACTIVE badge answers the wrong question.
    ...columns.filter((c) => c.id !== 'status'),
    {
      id: 'deleted_at',
      label: 'Deleted',
      header: 'Deleted',
      cell: (u) => (
        <span className="text-muted-foreground">
          {u.deleted_at ? new Date(u.deleted_at).toLocaleString() : '—'}
        </span>
      ),
      sortKey: 'deleted_at',
    },
  ]

  const liveColumns: Column<UserResponse>[] =
    canManageSessions || canInvite || canUpdate
      ? [
          ...columns,
          {
            id: 'actions',
            header: '',
            className: 'text-right',
            cell: (u) => {
              // The projection is platform-scoped: a group-only invitee has none, and resending
              // would mint one rather than re-send what the row shows.
              const showResend = canInvite && u.status === 'invited' && u.invitation != null
              const showRevoke = canInvite && u.invitation?.status === 'pending'
              // No self-logout from the admin list: revoking your own sessions here just
              // bounces you to /login. A dedicated self-logout is a separate concern.
              const showLogout = canManageSessions && u.id !== me?.id
              // A reset link only makes sense for an account that has a password to reset; the
              // backend 409s the rest. Sending yourself one is allowed — it costs nothing.
              const showReset = canUpdate && u.status === 'active'
              // Status is self-locked (the backend 403s it): deactivating yourself locks you out.
              // `invited`/`pending` are owned by the onboarding flows, so they offer no toggle.
              const showStatus =
                canUpdate && u.id !== me?.id && (u.status === 'active' || u.status === 'inactive')
              if (!showResend && !showRevoke && !showLogout && !showReset && !showStatus)
                return null
              return (
                <RowActions
                  menuLabel={`Actions for ${u.email}`}
                  actions={[
                    {
                      key: 'resend',
                      label: 'Resend invitation',
                      ariaLabel: `Resend invitation to ${u.email}`,
                      icon: RotateCw,
                      onSelect: () => resendInvitation.mutate(u.id),
                      disabled: resendInvitation.isPending && resendInvitation.variables === u.id,
                      when: showResend,
                    },
                    {
                      key: 'revoke',
                      label: 'Revoke invitation',
                      ariaLabel: `Revoke invitation of ${u.email}`,
                      icon: MailX,
                      onSelect: () => setRevokeTarget(u),
                      when: showRevoke,
                    },
                    {
                      key: 'logout',
                      label: 'Force logout',
                      ariaLabel: `Force logout ${u.email}`,
                      icon: LogOut,
                      onSelect: () => setLogoutTarget(u),
                      when: showLogout,
                    },
                    {
                      key: 'reset',
                      label: 'Send password reset link',
                      ariaLabel: `Send a password reset link to ${u.email}`,
                      icon: KeyRound,
                      onSelect: () => setResetTarget(u),
                      when: showReset,
                    },
                    {
                      key: 'deactivate',
                      label: 'Deactivate account',
                      ariaLabel: `Deactivate ${u.email}`,
                      icon: UserX,
                      onSelect: () => setDeactivateTarget(u),
                      destructive: true,
                      when: showStatus && u.status === 'active',
                    },
                    {
                      key: 'activate',
                      label: 'Activate account',
                      ariaLabel: `Activate ${u.email}`,
                      icon: UserCheck,
                      onSelect: () => changeStatus.mutate({ id: u.id, status: 'active' }),
                      disabled: changeStatus.isPending && changeStatus.variables?.id === u.id,
                      when: showStatus && u.status !== 'active',
                    },
                  ]}
                />
              )
            },
          },
        ]
      : columns
  const tableColumns = viewingDeleted ? deletedColumns : liveColumns

  return (
    <div className="space-y-6">
      <PageHeader title="Users" description="Platform user accounts." />
      <div className="flex flex-wrap gap-2">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            view.commitSearch()
          }}
          className="flex w-full gap-2 sm:max-w-sm sm:min-w-64 sm:flex-1"
        >
          <Input
            value={view.searchDraft}
            onChange={(e) => view.setSearchDraft(e.target.value)}
            placeholder="Search email…"
            aria-label="Search users by email"
            className="min-w-0 flex-1"
          />
          <Button type="submit" variant="outline">
            Search
          </Button>
        </form>
        <FilterSelect
          label="Status"
          value={view.filters.status}
          onChange={(v) => view.setFilter('status', v as UserStatus | '')}
          allLabel="All statuses"
          options={USER_STATUSES.map((s) => ({ value: s, label: s.replace(/_/g, ' ') }))}
        />
        <FilterSelect
          label="Role"
          value={view.filters.roleId}
          onChange={(v) => view.setFilter('roleId', v)}
          allLabel="All roles"
          options={(roles.data?.items ?? []).map((role) => ({
            value: role.id,
            label: role.display_name,
          }))}
        />
        {has('users:delete') && (
          <DeletedToggle
            value={view.filters.deleted}
            onChange={(next) => {
              view.setFilter('deleted', next)
              // Newest tombstone first entering, as the `deleted` param's hint recommends;
              // leaving, only a `deleted_at` sort is reset — it names a column the live
              // table doesn't have, while any other sort still means something there.
              view.setOrderBy(
                next
                  ? '-deleted_at'
                  : view.orderBy?.endsWith('deleted_at')
                    ? DEFAULT_ORDER_BY
                    : (view.orderBy ?? DEFAULT_ORDER_BY),
              )
            }}
            label="Which users to show"
          />
        )}
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="users" view={view} columns={columns} />
          {canInvite && !viewingDeleted && (
            <Button variant="outline" onClick={() => setInviteOpen(true)}>
              <Mail className="size-4" /> Invite
            </Button>
          )}
        </div>
      </div>
      {/* Mounted empty before the first selection — a region inserted with its text is never
          announced; the count alone, since a live toolbar re-announces all four buttons. */}
      {(canManageSessions || canUpdate) && (
        <span role="status" aria-live="polite" className="sr-only">
          {selectedKeys.size > 0 ? selectionLabel : ''}
        </span>
      )}
      {(canManageSessions || canUpdate) && selectedKeys.size > 0 && (
        <div
          role="group"
          aria-label="Bulk actions"
          className="bg-muted/40 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-sm"
        >
          <span className="font-medium">{selectionLabel}</span>
          {canManageSessions && (
            <Button size="sm" onClick={() => setBulkAction('logout')}>
              <LogOut className="size-4" /> Force logout
            </Button>
          )}
          {canUpdate && (
            <>
              <Button size="sm" onClick={() => setBulkAction('activate')}>
                <UserCheck className="size-4" /> Activate
              </Button>
              {/* Red before the confirm, unlike Force logout next to it: a revoked session can be
                  signed back into, a deactivated account cannot until someone reactivates it. */}
              <Button
                size="sm"
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                onClick={() => setBulkAction('deactivate')}
              >
                <UserX className="size-4" /> Deactivate
              </Button>
              <Button size="sm" onClick={() => setBulkAction('reset')}>
                <KeyRound className="size-4" /> Send password reset
              </Button>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSelectedKeys(new Set())}>
            Clear
          </Button>
        </div>
      )}
      <DataTable
        columns={tableColumns}
        rows={page?.items}
        rowKey={(u) => u.id}
        onRowClick={
          canUpdate && !viewingDeleted ? (u) => navigate(`/users/${u.id}/edit`) : undefined
        }
        isLoading={query.isPending || query.isPlaceholderData}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel={viewingDeleted ? 'Nothing deleted recently.' : 'No users yet.'}
        emptyHint={
          viewingDeleted
            ? 'Deleted accounts stay here for a limited time, then stop being restorable.'
            : 'Invite people by email to add them.'
        }
        emptyAction={
          canInvite && !viewingDeleted ? (
            <Button onClick={() => setInviteOpen(true)}>
              <Mail className="size-4" /> Invite
            </Button>
          ) : undefined
        }
        sort={{ by: view.orderBy, onChange: view.setOrderBy }}
        selection={
          (canManageSessions || canUpdate) && !viewingDeleted
            ? {
                selectedKeys,
                onSelectedChange: setSelectedKeys,
                rowSelectable: (u) => u.id !== me?.id,
              }
            : undefined
        }
      />
      {page && (
        <Pagination
          offset={view.offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={view.setOffset}
        />
      )}
      <InviteDialog open={inviteOpen} onOpenChange={setInviteOpen} />
      <ConfirmDialog
        open={logoutTarget !== null}
        onOpenChange={(o) => {
          if (!o) setLogoutTarget(null)
        }}
        title="Force logout"
        description={
          logoutTarget
            ? `Sign out ${logoutTarget.email}? All their active sessions are revoked immediately.`
            : ''
        }
        confirmLabel="Force logout"
        destructive
        pending={forceLogout.isPending}
        onConfirm={() => {
          if (!logoutTarget) return
          forceLogout.mutate(logoutTarget.id, { onSuccess: () => setLogoutTarget(null) })
        }}
      />
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(o) => {
          if (!o) setRevokeTarget(null)
        }}
        title="Revoke invitation"
        description={
          revokeTarget
            ? `Revoke the pending invitation of ${revokeTarget.email}? The accept link stops working immediately; the account stays and can be re-invited.`
            : ''
        }
        confirmLabel="Revoke"
        destructive
        pending={revokeInvitation.isPending}
        onConfirm={() => {
          if (!revokeTarget) return
          revokeInvitation.mutate(revokeTarget.id, { onSuccess: () => setRevokeTarget(null) })
        }}
      />
      <ConfirmDialog
        open={resetTarget !== null}
        onOpenChange={(o) => {
          if (!o) setResetTarget(null)
        }}
        title="Send password reset link"
        description={
          resetTarget
            ? `Mail a password reset link to ${resetTarget.email}? Any reset link they already have stops working.`
            : ''
        }
        confirmLabel="Send link"
        pending={sendPasswordReset.isPending}
        onConfirm={() => {
          if (!resetTarget) return
          sendPasswordReset.mutate(resetTarget.id, { onSuccess: () => setResetTarget(null) })
        }}
      />
      <ConfirmDialog
        open={deactivateTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeactivateTarget(null)
        }}
        title="Deactivate account"
        description={
          deactivateTarget
            ? `Deactivate ${deactivateTarget.email}? They are signed out immediately and cannot sign in until the account is reactivated.`
            : ''
        }
        confirmLabel="Deactivate"
        destructive
        pending={changeStatus.isPending}
        onConfirm={() => {
          if (!deactivateTarget) return
          changeStatus.mutate(
            { id: deactivateTarget.id, status: 'inactive' },
            { onSuccess: () => setDeactivateTarget(null) },
          )
        }}
      />
      <ConfirmDialog
        open={bulkAction !== null}
        onOpenChange={(o) => {
          if (!o) setBulkAction(null)
        }}
        title={pendingBulk?.title ?? ''}
        description={pendingBulk?.description ?? ''}
        confirmLabel={pendingBulk?.confirmLabel ?? ''}
        destructive={pendingBulk?.destructive ?? false}
        pending={bulkPending}
        onConfirm={() => {
          if (bulkAction) runBulk(bulkAction)
        }}
      />
      <BulkResultDialog
        open={bulkResult !== null}
        onOpenChange={(o) => {
          if (!o) setBulkResult(null)
        }}
        title={bulkResult?.title ?? ''}
        result={bulkResult?.result}
        labelFor={(key) => bulkResult?.labels.get(key) ?? key}
      />
    </div>
  )
}
