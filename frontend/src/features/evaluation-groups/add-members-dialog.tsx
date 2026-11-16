import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useBulkAddGroupMembers } from './mutations'
import { useAllGroupMemberIds } from './queries'
import {
  ALL_MEMBERS_CAP,
  useAllOrganizationMembers,
  useOrganization,
} from '@/features/organizations/queries'
import { useInfiniteUsers, useRoles } from '@/features/users/queries'
import { fullName } from '@/features/users/names'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { BulkGroupMemberResponse, UserResponse } from '@/lib/api/types'

type Picked = Pick<UserResponse, 'id' | 'email' | 'first_name' | 'last_name'>

const DEFAULT_ROLE = 'red_teamer'
// Least to most privileged, as on the wireframe; any other assignable role follows.
const ROLE_ORDER = ['viewer', 'red_teamer', 'annotator', 'owner']
const rank = (name: string) => {
  const i = ROLE_ORDER.indexOf(name)
  return i === -1 ? ROLE_ORDER.length : i
}

const label = (u: Picked) => [fullName(u), u.email].filter(Boolean).join(' · ')

function UserRow({
  user,
  isMember,
  checked,
  onToggle,
}: {
  user: Picked
  isMember: boolean
  checked: boolean
  onToggle: () => void
}) {
  return (
    <label className="flex items-center gap-2 px-3 py-1.5 text-sm">
      <Checkbox checked={checked || isMember} disabled={isMember} onCheckedChange={onToggle} />
      <span className="min-w-0 flex-1 truncate">{label(user)}</span>
      {isMember && <span className="text-muted-foreground text-xs">Already a member</span>}
    </label>
  )
}

export function AddMembersDialog({
  groupId,
  organizationId,
  canPickOrganization,
  canPickUsers,
  open,
  onOpenChange,
}: {
  groupId: string
  organizationId: string | null | undefined
  canPickOrganization: boolean
  canPickUsers: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [step, setStep] = useState<'users' | 'roles' | 'result'>('users')
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<Map<string, Picked>>(new Map())
  const [roleIds, setRoleIds] = useState<Record<string, string[]>>({})
  const [result, setResult] = useState<BulkGroupMemberResponse | null>(null)

  const showOrganization = canPickOrganization && !!organizationId
  const org = useOrganization(organizationId ?? '', {
    enabled: open && showOrganization,
    suppressErrorToast: true,
  })
  const orgMembers = useAllOrganizationMembers(organizationId ?? '', {
    enabled: open && showOrganization,
  })
  const term = search.trim()
  const users = useInfiniteUsers(
    { limit: 50, search: term || undefined, order_by: 'email' },
    { enabled: open && canPickUsers, suppressErrorToast: true },
  )
  const memberIds = useAllGroupMemberIds(groupId, { enabled: open })
  const roles = useRoles({ objectAssignable: true, enabled: open })
  const add = useBulkAddGroupMembers(groupId)

  const members = new Set(memberIds.data)
  const matches = (u: Picked) => term === '' || label(u).toLowerCase().includes(term.toLowerCase())
  const orgList = orgMembers.data?.items ?? []
  const orgIds = new Set(orgList.map((u) => u.id))
  const orgVisible = orgList.filter(matches)
  const orgJoinable = orgList.filter((u) => !members.has(u.id))
  const orgPicked = orgJoinable.filter((u) => picked.has(u.id)).length
  const others = (users.data ?? []).filter((u) => !orgIds.has(u.id))
  const roleList = [...(roles.data?.items ?? [])].sort((a, b) => rank(a.name) - rank(b.name))
  // The bulk route rejects a larger request whole (422), so stop it here with a reason.
  const tooMany = picked.size > ALL_MEMBERS_CAP

  function toggle(user: Picked) {
    setPicked((prev) => {
      const next = new Map(prev)
      if (next.has(user.id)) next.delete(user.id)
      else next.set(user.id, user)
      return next
    })
  }

  function toggleOrganization() {
    setPicked((prev) => {
      const next = new Map(prev)
      const all = orgJoinable.every((u) => next.has(u.id))
      for (const u of orgJoinable) {
        if (all) next.delete(u.id)
        else next.set(u.id, u)
      }
      return next
    })
  }

  function toRoles() {
    const fallback = roleList.find((r) => r.name === DEFAULT_ROLE)
    // Keep what was set on an earlier pass through this step; a newly picked user starts as Red Teamer.
    setRoleIds((prev) =>
      Object.fromEntries(
        [...picked.keys()].map((id) => [id, prev[id] ?? (fallback ? [fallback.id] : [])]),
      ),
    )
    setStep('roles')
  }

  function toggleRole(userId: string, roleId: string) {
    setRoleIds((prev) => {
      const held = prev[userId] ?? []
      return {
        ...prev,
        [userId]: held.includes(roleId) ? held.filter((r) => r !== roleId) : [...held, roleId],
      }
    })
  }

  const pickedList = [...picked.values()]
  const allHold = (roleId: string) =>
    pickedList.length > 0 && pickedList.every((u) => roleIds[u.id]?.includes(roleId))
  const someHold = (roleId: string) => pickedList.some((u) => roleIds[u.id]?.includes(roleId))

  function toggleRoleForAll(roleId: string) {
    const on = !allHold(roleId)
    setRoleIds((prev) =>
      Object.fromEntries(
        pickedList.map((u) => {
          const held = (prev[u.id] ?? []).filter((r) => r !== roleId)
          return [u.id, on ? [...held, roleId] : held]
        }),
      ),
    )
  }

  const missingRole = pickedList.some((u) => (roleIds[u.id] ?? []).length === 0)

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  async function submit() {
    let data: BulkGroupMemberResponse
    try {
      data = await add.mutateAsync({
        dry_run: false,
        rows: pickedList.map((u) => ({
          row_key: u.id,
          data: { user_id: u.id, role_ids: roleIds[u.id] ?? [] },
        })),
      })
    } catch {
      return // The global mutation handler has already toasted it.
    }
    if (data.failed === 0) close()
    else if (mounted.current) {
      setResult(data)
      setStep('result')
    } else {
      // The page went away mid-request, so the result step has nowhere to render.
      toast.warning(
        `Added ${data.succeeded} of ${data.total} members; ${data.failed} could not be added.`,
      )
    }
  }

  // Reset on close, not via `onOpen`: that runs after the first open render.
  function close() {
    setStep('users')
    setSearch('')
    setPicked(new Map())
    setRoleIds({})
    setResult(null)
    onOpenChange(false)
  }

  const orgName = org.data?.name ?? 'Organization'

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="Add member(s)"
      busy={add.isPending}
      className="max-w-2xl"
    >
      {step === 'users' && (
        <>
          <div className="space-y-2">
            <Label htmlFor="add-members-search">Users</Label>
            <Input
              id="add-members-search"
              placeholder="Search by name or email"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <div className="max-h-80 overflow-y-auto rounded-md border py-1">
              {showOrganization && (
                <section aria-labelledby="add-members-org">
                  <p id="add-members-org" className="px-3 pt-1 pb-1 text-sm font-semibold">
                    {orgName} (organization)
                  </p>
                  {orgMembers.isPending && (
                    <p className="text-muted-foreground px-3 py-1.5 text-sm">Loading members…</p>
                  )}
                  {orgMembers.isError && (
                    <p className="text-destructive px-3 py-1.5 text-sm">
                      Couldn’t load the organization’s members.
                    </p>
                  )}
                  {orgMembers.isSuccess && orgJoinable.length > 0 && (
                    <label className="bg-muted/60 flex items-center gap-2 px-3 py-1.5 text-sm font-medium">
                      <Checkbox
                        checked={
                          orgPicked === orgJoinable.length
                            ? true
                            : orgPicked > 0
                              ? 'indeterminate'
                              : false
                        }
                        onCheckedChange={toggleOrganization}
                      />
                      Add all members
                    </label>
                  )}
                  {orgMembers.isSuccess && orgList.length === 0 && (
                    <p className="text-muted-foreground px-3 py-1.5 text-sm">
                      This organization has no members.
                    </p>
                  )}
                  {orgVisible.map((u) => (
                    <UserRow
                      key={u.id}
                      user={u}
                      isMember={members.has(u.id)}
                      checked={picked.has(u.id)}
                      onToggle={() => toggle(u)}
                    />
                  ))}
                  {(orgMembers.data?.total ?? 0) > ALL_MEMBERS_CAP && (
                    <p className="text-muted-foreground px-3 py-1.5 text-xs">
                      Showing the first {ALL_MEMBERS_CAP} of {orgMembers.data?.total} members.
                    </p>
                  )}
                </section>
              )}
              {canPickUsers && (
                <section aria-labelledby={showOrganization ? 'add-members-others' : undefined}>
                  {showOrganization && (
                    <p id="add-members-others" className="px-3 pt-3 pb-1 text-sm font-semibold">
                      Other members
                    </p>
                  )}
                  {users.isLoading && (
                    <p className="text-muted-foreground px-3 py-1.5 text-sm">Loading users…</p>
                  )}
                  {users.isError && (
                    <p className="text-destructive px-3 py-1.5 text-sm">
                      {users.isFetchNextPageError
                        ? 'Couldn’t load more users.'
                        : 'Couldn’t load users.'}
                    </p>
                  )}
                  {users.isSuccess && others.length === 0 && !users.hasNextPage && (
                    <p className="text-muted-foreground px-3 py-1.5 text-sm">No matching users.</p>
                  )}
                  {others.map((u) => (
                    <UserRow
                      key={u.id}
                      user={u}
                      isMember={members.has(u.id)}
                      checked={picked.has(u.id)}
                      onToggle={() => toggle(u)}
                    />
                  ))}
                  {users.hasNextPage && (
                    <div className="px-3 py-1.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={users.isFetchingNextPage}
                        onClick={() => void users.fetchNextPage({ cancelRefetch: false })}
                      >
                        {users.isFetchingNextPage ? 'Loading…' : 'Load more'}
                      </Button>
                    </div>
                  )}
                </section>
              )}
            </div>
          </div>
          {tooMany && (
            <p className="text-muted-foreground text-xs">
              One add takes at most {ALL_MEMBERS_CAP} users — {picked.size} are selected.
            </p>
          )}
          {roles.isError && (
            <p className="text-destructive text-xs">Couldn’t load the roles to assign.</p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button
              disabled={picked.size === 0 || tooMany || !roles.isSuccess || !memberIds.isSuccess}
              onClick={toRoles}
            >
              Next{picked.size > 0 && ` (${picked.size} selected)`}
            </Button>
          </div>
        </>
      )}

      {step === 'roles' && (
        <>
          <p className="text-muted-foreground text-sm">
            Choose the roles each user gets in this group.
          </p>
          <div className="max-h-96 overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th
                    scope="col"
                    className="bg-card sticky left-0 px-3 py-2 text-left font-semibold"
                  >
                    User
                  </th>
                  {roleList.map((r) => (
                    <th key={r.id} scope="col" className="px-2 py-2 text-center font-semibold">
                      {r.display_name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr className="bg-muted/60">
                  <th
                    scope="row"
                    className="bg-muted sticky left-0 px-3 py-2 text-left font-medium"
                  >
                    Select all
                  </th>
                  {roleList.map((r) => (
                    <td key={r.id} className="px-2 py-2 text-center">
                      <Checkbox
                        aria-label={`Select all as ${r.display_name}`}
                        checked={allHold(r.id) ? true : someHold(r.id) ? 'indeterminate' : false}
                        onCheckedChange={() => toggleRoleForAll(r.id)}
                      />
                    </td>
                  ))}
                </tr>
                {pickedList.map((u) => (
                  <tr key={u.id} className="border-t">
                    <th
                      scope="row"
                      className="bg-card sticky left-0 max-w-56 truncate px-3 py-2 text-left font-normal"
                    >
                      {label(u)}
                    </th>
                    {roleList.map((r) => (
                      <td key={r.id} className="px-2 py-2 text-center">
                        <Checkbox
                          aria-label={`${r.display_name} for ${label(u)}`}
                          checked={roleIds[u.id]?.includes(r.id) ?? false}
                          onCheckedChange={() => toggleRole(u.id, r.id)}
                        />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {missingRole && (
            <p className="text-muted-foreground text-xs">Give every user at least one role.</p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setStep('users')} disabled={add.isPending}>
              Back
            </Button>
            <Button disabled={missingRole || add.isPending} onClick={() => void submit()}>
              {add.isPending ? 'Adding…' : `Add ${pickedList.length}`}
            </Button>
          </div>
        </>
      )}

      {step === 'result' && result && (
        <>
          <div className="space-y-2 text-sm" role="status">
            <p>
              Added {result.succeeded} of {result.total}. These could not be added:
            </p>
            <ul className="space-y-1">
              {result.results
                .filter((r) => r.status === 'failed')
                .map((r) => (
                  <li key={r.row_key}>
                    <span className="font-medium">{picked.get(r.row_key)?.email ?? r.row_key}</span>
                    {' — '}
                    <span className="text-muted-foreground">
                      {/* The 409 detail is the object-role layer's wording; say it as step 1 does. */}
                      {r.error?.status === 409
                        ? 'Already a member'
                        : (r.error?.detail ?? r.error?.title ?? 'Unknown error')}
                    </span>
                  </li>
                ))}
            </ul>
          </div>
          <div className="flex justify-end">
            <Button onClick={close}>Done</Button>
          </div>
        </>
      )}
    </Modal>
  )
}
