import { useState } from 'react'
import { Mail, Pencil, Plus, Trash2 } from 'lucide-react'
import { useGroupMembers, useGroupAnnotators } from './queries'
import { useAddGroupMember, useRemoveGroupMember, useUpdateGroupMember } from './mutations'
import { BulkInviteDialog } from './bulk-invite-dialog'
import { RolesPicker } from '@/features/users/roles-picker'
import { useUsers } from '@/features/users/queries'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { useObjectPermissions, usePermissions } from '@/lib/auth/use-permissions'
import type { EvaluationGroupAccessLevel, ObjectMemberResponse } from '@/lib/api/types'

function displayName(user: {
  first_name?: string | null
  last_name?: string | null
  email: string
}) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(' ')
  return name || user.email
}

function AddMemberDialog({
  groupId,
  open,
  onOpenChange,
}: {
  groupId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const users = useUsers({ limit: 100, offset: 0 })
  const add = useAddGroupMember(groupId)
  const [userId, setUserId] = useState('')
  const [roleIds, setRoleIds] = useState<string[]>([])

  const canSubmit = userId !== '' && roleIds.length > 0 && !add.isPending

  function reset() {
    setUserId('')
    setRoleIds([])
  }

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Add member" onOpen={reset}>
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="member-user">User</Label>
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger id="member-user">
              <SelectValue placeholder="— select user —" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(users.data?.items ?? []).map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.email}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Roles</Label>
          <RolesPicker selected={roleIds} onChange={setRoleIds} objectAssignableOnly />
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={add.isPending}>
          Cancel
        </Button>
        <Button
          disabled={!canSubmit}
          onClick={() =>
            add.mutate(
              { user_id: userId, role_ids: roleIds },
              { onSuccess: () => onOpenChange(false) },
            )
          }
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

export function MembersSection({
  groupId,
  accessLevel,
  userPermissions,
}: {
  groupId: string
  accessLevel: EvaluationGroupAccessLevel
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
  // The annotator list is manage-members-gated server-side; only fetch it when the
  // caller can manage, so a plain member doesn't 403 (and toast) on it.
  const annotators = useGroupAnnotators(groupId, { enabled: canManageMembers })
  // "Add existing user" additionally needs the global user picker (`/auth/users`,
  // gated on `users:read`); invite-by-email doesn't.
  const canAddExisting = canManageMembers && hasGlobal('users:read')
  const [addOpen, setAddOpen] = useState(false)
  const [inviteOpen, setInviteOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<ObjectMemberResponse | null>(null)
  const [editTarget, setEditTarget] = useState<ObjectMemberResponse | null>(null)

  const memberList = members.data?.items ?? []
  const annotatorList = annotators.data?.items ?? []

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

      {canManageMembers && (
        <div className="space-y-2">
          <h3 className="text-muted-foreground text-sm font-medium">Annotators</h3>
          <p className="text-muted-foreground text-xs">
            {accessLevel === 'public'
              ? 'Reviewers assignable to flags here — every platform annotator, not just this group’s members.'
              : 'Members holding the annotator role — assignable as reviewers for flags here.'}
          </p>
          {annotators.isError && (
            <p className="text-muted-foreground text-sm">
              You don’t have access to the annotator list.
            </p>
          )}
          {annotatorList.length === 0 && !annotators.isPending && !annotators.isError && (
            <p className="text-muted-foreground text-sm">No annotators.</p>
          )}
          {annotatorList.map((a) => (
            <div key={a.id} className="text-sm">
              {displayName(a)}
            </div>
          ))}
        </div>
      )}

      {canAddExisting && (
        <AddMemberDialog groupId={groupId} open={addOpen} onOpenChange={setAddOpen} />
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
