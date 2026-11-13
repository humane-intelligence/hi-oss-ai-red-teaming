import { useState } from 'react'
import { Mail, Pencil, Plus, Trash2 } from 'lucide-react'
import { useGroupMembers } from './queries'
import { useAddGroupMember, useRemoveGroupMember, useUpdateGroupMember } from './mutations'
import { BulkInviteDialog } from './bulk-invite-dialog'
import { RolesPicker } from '@/features/users/roles-picker'
import { useInfiniteUsers } from '@/features/users/queries'
import { useOrganization } from '@/features/organizations/queries'
import { displayName, fullName } from '@/features/users/names'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { SearchSelect } from '@/components/shared/search-select'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { useObjectPermissions, usePermissions } from '@/lib/auth/use-permissions'
import type { EvaluationGroupAccessLevel, ObjectMemberResponse } from '@/lib/api/types'

function AddMemberDialog({
  groupId,
  currentMemberIds,
  open,
  onOpenChange,
}: {
  groupId: string
  currentMemberIds: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [search, setSearch] = useState('')
  const users = useInfiniteUsers(
    { limit: 50, search: search.trim() || undefined, order_by: 'email' },
    { enabled: open },
  )
  const add = useAddGroupMember(groupId)
  const [userId, setUserId] = useState('')
  const [roleIds, setRoleIds] = useState<string[]>([])
  // The modal stays mounted while closed; a fresh key drops the picker's typed term on reopen.
  const [pickerKey, setPickerKey] = useState(0)

  const canSubmit = userId !== '' && roleIds.length > 0 && !add.isPending

  const members = new Set(currentMemberIds)
  const options = (users.data ?? []).map((u) => {
    const isMember = members.has(u.id)
    return {
      value: u.id,
      label: [fullName(u), u.email].filter(Boolean).join(' · '),
      disabled: isMember,
      hint: isMember ? 'Already a member' : undefined,
    }
  })

  // Reset on close, not via `onOpen`: that runs after the first open render, which would
  // refetch the previous term once more.
  function close() {
    setSearch('')
    setUserId('')
    setRoleIds([])
    setPickerKey((k) => k + 1)
    onOpenChange(false)
  }

  return (
    <Modal open={open} onOpenChange={close} title="Add member">
      <div className="space-y-3">
        <SearchSelect
          key={pickerKey}
          label="User"
          htmlFor="member-user"
          value={userId}
          onChange={setUserId}
          onSearchChange={setSearch}
          isPending={users.isLoading}
          // A failed next page keeps the loaded ones listed; scrolling to the end retries it.
          isError={users.isError && !users.isFetchNextPageError}
          emptyLabel="No matching users."
          options={options}
          // Don't cancel a background refetch of the pages already loaded.
          onLoadMore={() => void users.fetchNextPage({ cancelRefetch: false })}
          hasMore={users.hasNextPage}
          isLoadingMore={users.isFetchingNextPage}
        />
        <div className="space-y-1.5">
          <Label>Roles</Label>
          <RolesPicker selected={roleIds} onChange={setRoleIds} objectAssignableOnly />
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={close} disabled={add.isPending}>
          Cancel
        </Button>
        <Button
          disabled={!canSubmit}
          onClick={() => add.mutate({ user_id: userId, role_ids: roleIds }, { onSuccess: close })}
        >
          {add.isPending ? 'Adding…' : 'Add'}
        </Button>
      </div>
    </Modal>
  )
}

function EditMemberDialog({
  groupId,
  member,
  open,
  onOpenChange,
}: {
  groupId: string
  member: ObjectMemberResponse | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const update = useUpdateGroupMember(groupId)
  const [roleIds, setRoleIds] = useState<string[]>([])

  const canSubmit = roleIds.length > 0 && !update.isPending

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Edit roles"
      onOpen={() => setRoleIds(member?.roles.map((r) => r.id) ?? [])}
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label>Roles</Label>
          <RolesPicker selected={roleIds} onChange={setRoleIds} objectAssignableOnly />
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={update.isPending}>
          Cancel
        </Button>
        <Button
          disabled={!canSubmit}
          onClick={() =>
            member &&
            update.mutate(
              { user_id: member.user.id, role_ids: roleIds },
              { onSuccess: () => onOpenChange(false) },
            )
          }
        >
          {update.isPending ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </Modal>
  )
}

// Members of the owning organization can join an organization-access group themselves; without
// this the page gave no hint of it. Public groups are open to everyone, invitation-only to nobody.
function EligibleToJoin({ organizationId }: { organizationId: string }) {
  const { has } = usePermissions()
  const org = useOrganization(organizationId, {
    enabled: has('organizations:read'),
    suppressErrorToast: true,
  })
  return (
    <section aria-labelledby="eligible-to-join" className="space-y-2">
      <h3 id="eligible-to-join" className="font-semibold">
        Eligible to join on their own (1)
      </h3>
      <div className="bg-card rounded-md border px-3 py-2">
        <div className="font-medium">{org.data?.name ?? 'The owning organization'}</div>
        <div className="text-muted-foreground text-sm">
          Organization — its members can join this group themselves
        </div>
      </div>
    </section>
  )
}

export function MembersSection({
  groupId,
  accessLevel,
  organizationId,
  userPermissions,
}: {
  groupId: string
  accessLevel: EvaluationGroupAccessLevel
  organizationId: string | null | undefined
  userPermissions: readonly string[]
}) {
  const members = useGroupMembers(groupId)
  const remove = useRemoveGroupMember(groupId)
  const { has: hasGlobal } = usePermissions()
  // Member management is object-scoped: the server grants it via the in-group
  // `manage_members` (owner) or break-glass `manage`. Gate on the group's
  // effective permissions, not the caller's global union.
  const groupPerms = useObjectPermissions(userPermissions)
  const canManageMembers = groupPerms.hasAny([
    'evaluation_groups:manage',
    'evaluation_groups:manage_members',
  ])
  // "Add existing user" additionally needs the global user picker (`/auth/users`,
  // gated on `users:read`); invite-by-email doesn't.
  const canAddExisting = canManageMembers && hasGlobal('users:read')
  const [addOpen, setAddOpen] = useState(false)
  const [inviteOpen, setInviteOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<ObjectMemberResponse | null>(null)
  const [editTarget, setEditTarget] = useState<ObjectMemberResponse | null>(null)

  const memberList = members.data?.items ?? []

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Members ({memberList.length})</h2>
        {canManageMembers && (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setInviteOpen(true)}>
              <Mail className="size-4" /> Invite by email
            </Button>
            {canAddExisting && (
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="size-4" /> Add member
              </Button>
            )}
          </div>
        )}
      </div>

      {members.isError && (
        <p className="text-destructive">Error: {(members.error as Error).message}</p>
      )}

      <div className="space-y-2">
        {memberList.map((m) => (
          <div
            key={m.user.id}
            className="bg-card flex items-center gap-3 rounded-md border px-3 py-2"
          >
            <div className="min-w-0 flex-1">
              <div className="font-medium">{displayName(m.user)}</div>
              {fullName(m.user) && (
                <div className="text-muted-foreground text-sm">{m.user.email}</div>
              )}
              <div className="text-muted-foreground text-sm">
                {m.roles.map((r) => r.display_name).join(', ')}
              </div>
            </div>
            {canManageMembers && (
              <>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Edit roles"
                  onClick={() => setEditTarget(m)}
                >
                  <Pencil className="size-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Remove member"
                  onClick={() => setRemoveTarget(m)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </>
            )}
          </div>
        ))}
        {!members.isPending && memberList.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No members yet. Add red-teamers and reviewers to give them access to this group.
          </p>
        )}
      </div>

      {accessLevel === 'organization' && organizationId && (
        <EligibleToJoin organizationId={organizationId} />
      )}

      {canAddExisting && (
        <AddMemberDialog
          groupId={groupId}
          currentMemberIds={memberList.map((m) => m.user.id)}
          open={addOpen}
          onOpenChange={setAddOpen}
        />
      )}
      <BulkInviteDialog groupId={groupId} open={inviteOpen} onOpenChange={setInviteOpen} />
      <EditMemberDialog
        groupId={groupId}
        member={editTarget}
        open={editTarget !== null}
        onOpenChange={(o) => {
          if (!o) setEditTarget(null)
        }}
      />
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(o) => {
          if (!o) setRemoveTarget(null)
        }}
        title="Remove member"
        description={
          removeTarget
            ? `Remove ${displayName(removeTarget.user)} from this group? You can undo this from the toast.`
            : undefined
        }
        confirmLabel="Remove"
        destructive
        pending={remove.isPending}
        onConfirm={() =>
          removeTarget &&
          remove.mutate(
            {
              userId: removeTarget.user.id,
              // Captured before the removal: Undo re-adds exactly the roles the row held.
              roleIds: removeTarget.roles.map((r) => r.id),
            },
            { onSuccess: () => setRemoveTarget(null) },
          )
        }
      />
    </div>
  )
}
