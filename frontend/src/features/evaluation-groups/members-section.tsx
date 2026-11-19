import { useState } from 'react'
import { Mail, Pencil, Plus, Trash2 } from 'lucide-react'
import { useGroupAssignableRoles, useGroupMembers } from './queries'
import { useRemoveGroupMember, useUpdateGroupMember } from './mutations'
import { BulkInviteDialog } from './bulk-invite-dialog'
import { AddMembersDialog } from './add-members-dialog'
import { RolesPicker } from '@/features/users/roles-picker'
import { useOrganization } from '@/features/organizations/queries'
import { displayName, fullName } from '@/features/users/names'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Pagination } from '@/components/shared/pagination'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { useObjectPermissions, usePermissions } from '@/lib/auth/use-permissions'
import type { EvaluationGroupAccessLevel, ObjectMemberResponse } from '@/lib/api/types'

const PAGE_SIZE = 50

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
  const roles = useGroupAssignableRoles(groupId, { enabled: open })
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
          <RolesPicker
            roles={roles.data?.items ?? []}
            isPending={roles.isPending}
            isError={roles.isError}
            selected={roleIds}
            onChange={setRoleIds}
          />
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

// Only an organization-access group has a bounded set of self-joiners worth naming: a public group is
// open to everyone, an invitation-only one to nobody.
function EligibleToJoin({ organizationId }: { organizationId: string }) {
  const { has } = usePermissions()
  const org = useOrganization(organizationId, {
    enabled: has('organizations:read'),
    suppressErrorToast: true,
  })
  return (
    <section aria-labelledby="eligible-to-join" className="space-y-2">
      <h3 id="eligible-to-join" className="font-semibold">
        Eligible to join on their own
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
  const [offset, setOffset] = useState(0)
  const members = useGroupMembers(groupId, { limit: PAGE_SIZE, offset, keepPrevious: true })
  const remove = useRemoveGroupMember(groupId)
  const { has: hasGlobal } = usePermissions()
  // Member management is object-scoped: the server takes the in-group `manage_members` or the
  // global break-glass, whose effective group set carries `manage_members` too. An in-group
  // `manage` alone is not enough there, so it isn't here.
  const groupPerms = useObjectPermissions(userPermissions)
  const canManageMembers = groupPerms.has('evaluation_groups:manage_members')
  // "Add existing user" additionally needs the global user picker (`/auth/users`,
  // gated on `users:read`); invite-by-email doesn't.
  const canPickUsers = hasGlobal('users:read')
  // Organization members come from `/organizations/{id}/members` (`organizations:read`).
  const canPickOrganization = hasGlobal('organizations:read') && !!organizationId
  const canAddExisting = canManageMembers && (canPickUsers || canPickOrganization)
  const [addOpen, setAddOpen] = useState(false)
  const [inviteOpen, setInviteOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<ObjectMemberResponse | null>(null)
  const [editTarget, setEditTarget] = useState<ObjectMemberResponse | null>(null)

  const memberList = members.data?.items ?? []

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Members ({members.data?.total ?? 0})</h2>
        {canManageMembers && (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setInviteOpen(true)}>
              <Mail className="size-4" /> Invite by email
            </Button>
            {canAddExisting && (
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="size-4" /> Add member(s)
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
        {/* Past the first page the pager stays, so removing the last row there can step back. */}
        {members.data && (members.data.total > PAGE_SIZE || offset > 0) && (
          <Pagination
            offset={offset}
            limit={PAGE_SIZE}
            total={members.data.total}
            onOffsetChange={setOffset}
          />
        )}
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
        <AddMembersDialog
          groupId={groupId}
          organizationId={organizationId}
          canPickOrganization={canPickOrganization}
          canPickUsers={canPickUsers}
          open={addOpen}
          onOpenChange={setAddOpen}
        />
      )}
      <BulkInviteDialog groupId={groupId} open={inviteOpen} onOpenChange={setInviteOpen} />
      {canManageMembers && (
        <EditMemberDialog
          groupId={groupId}
          member={editTarget}
          open={editTarget !== null}
          onOpenChange={(o) => {
            if (!o) setEditTarget(null)
          }}
        />
      )}
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
