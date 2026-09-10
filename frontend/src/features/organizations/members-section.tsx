import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { useOrganizationMembers } from './queries'
import { useAddOrganizationMember, useRemoveOrganizationMember } from './mutations'
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
import { usePermissions } from '@/lib/auth/use-permissions'
import type { UserResponse } from '@/lib/api/types'

function displayName(user: {
  first_name?: string | null
  last_name?: string | null
  email: string
}) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(' ')
  return name || user.email
}

function AddMemberDialog({
  organizationId,
  currentMemberIds,
  open,
  onOpenChange,
}: {
  organizationId: string
  currentMemberIds: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const users = useUsers({ limit: 100, offset: 0 })
  const add = useAddOrganizationMember(organizationId)
  const [userId, setUserId] = useState('')

  const canSubmit = userId !== '' && !add.isPending

  // Already-assigned users can't be re-added; other-org users stay (assigning moves them).
  const taken = new Set(currentMemberIds)
  const candidates = (users.data?.items ?? []).filter((u) => !taken.has(u.id))

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Add member" onOpen={() => setUserId('')}>
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="member-user">User</Label>
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger id="member-user">
              <SelectValue placeholder="— select user —" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {candidates.map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.email}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </div>
        <p className="text-muted-foreground text-xs">
          A user belongs to one organization — assigning them here moves them out of any
          organization they were in before.
        </p>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={add.isPending}>
          Cancel
        </Button>
        <Button
          disabled={!canSubmit}
          onClick={() => add.mutate({ user_id: userId }, { onSuccess: () => onOpenChange(false) })}
        >
          {add.isPending ? 'Adding…' : 'Add'}
        </Button>
      </div>
    </Modal>
  )
}

export function MembersSection({ organizationId }: { organizationId: string }) {
  const members = useOrganizationMembers(organizationId)
  const remove = useRemoveOrganizationMember(organizationId)
  const { has } = usePermissions()
  const canManageMembers = has('organizations:manage_members')
  // "Add existing user" needs the user picker (`/auth/users`).
  const canAddExisting = canManageMembers && has('users:read')
  const [addOpen, setAddOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<UserResponse | null>(null)

  const memberList = members.data?.items ?? []

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-lg font-semibold tracking-tight">
          Members ({members.data?.total ?? 0})
        </h2>
        {canAddExisting && (
          <Button size="sm" onClick={() => setAddOpen(true)}>
            <Plus className="size-4" /> Add member
          </Button>
        )}
      </div>

      {members.isError && (
        <p className="text-destructive">Error: {(members.error as Error).message}</p>
      )}

      <div className="space-y-2">
        {memberList.map((m) => (
          <div key={m.id} className="bg-card flex items-center gap-3 rounded-md border px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="font-medium">{displayName(m)}</div>
              <div className="text-muted-foreground text-sm">{m.email}</div>
            </div>
            {canManageMembers && (
              <Button
                variant="ghost"
                size="icon"
                aria-label="Remove member"
                onClick={() => setRemoveTarget(m)}
              >
                <Trash2 className="size-4" />
              </Button>
            )}
          </div>
        ))}
        {!members.isPending && memberList.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No members yet. Assign users to this organization to group them under it.
          </p>
        )}
      </div>

      {canAddExisting && (
        <AddMemberDialog
          organizationId={organizationId}
          currentMemberIds={memberList.map((m) => m.id)}
          open={addOpen}
          onOpenChange={setAddOpen}
        />
      )}
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(o) => {
          if (!o) setRemoveTarget(null)
        }}
        title="Remove member"
        description={
          removeTarget ? `Remove ${displayName(removeTarget)} from this organization?` : undefined
        }
        confirmLabel="Remove"
        destructive
        pending={remove.isPending}
        onConfirm={() =>
          removeTarget && remove.mutate(removeTarget.id, { onSuccess: () => setRemoveTarget(null) })
        }
      />
    </div>
  )
}
