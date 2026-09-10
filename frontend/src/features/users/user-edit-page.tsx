import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Trash2 } from 'lucide-react'
import { useUser } from './queries'
import { useDeleteUser, useUpdateUser } from './mutations'
import { RolesPicker } from './roles-picker'
import { StatusPill } from '@/components/shared/status-pill'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { humanizeError } from '@/lib/api/problem'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { PageHeader } from '@/components/shared/page-header'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent } from '@/components/ui/card'
import type { UserResponse } from '@/lib/api/types'

export function UserEditPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const user = useUser(id ?? '')

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/users')}>
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title="Edit user"
        description={user.data?.email}
        breadcrumbs={
          <Breadcrumbs items={[{ label: 'Users', to: '/users' }, { label: 'Edit user' }]} />
        }
      />

      {user.isPending && <DetailSkeleton />}
      {user.isError && <p className="text-destructive">{humanizeError(user.error)}</p>}

      {/* keyed so the form seeds its state from props once per user */}
      {user.data && (
        <UserEditForm key={user.data.id} user={user.data} onDone={() => navigate('/users')} />
      )}
    </div>
  )
}

function UserEditForm({ user, onDone }: { user: UserResponse; onDone: () => void }) {
  const update = useUpdateUser(user.id)
  const del = useDeleteUser()
  const [firstName, setFirstName] = useState(user.first_name ?? '')
  const [lastName, setLastName] = useState(user.last_name ?? '')
  const initialRoleIds = (user.roles ?? []).map((r) => r.id)
  const [roleIds, setRoleIds] = useState<string[]>(initialRoleIds)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const dirty =
    firstName !== (user.first_name ?? '') ||
    lastName !== (user.last_name ?? '') ||
    [...roleIds].sort().join() !== [...initialRoleIds].sort().join()
  useUnsavedGuard(dirty)

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="first_name">First name</Label>
            <Input
              id="first_name"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="last_name">Last name</Label>
            <Input id="last_name" value={lastName} onChange={(e) => setLastName(e.target.value)} />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>Organization</Label>
          {/* Read-only: membership is assigned from the organization's members, not here. */}
          <p className="text-sm">
            {user.organization ? (
              <Link
                to={`/organizations/${user.organization.id}`}
                className="hover:text-foreground hover:underline"
              >
                {user.organization.name}
              </Link>
            ) : (
              <span className="text-muted-foreground">No organization</span>
            )}
          </p>
          <p className="text-muted-foreground text-xs">Managed from the organization’s members.</p>
        </div>
        {/* Read-only, and the only surface for these two: the list hides both below `xl`. */}
        <div className="space-y-1.5">
          <Label>Account state</Label>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="flex items-center gap-1.5">
              <span className="text-muted-foreground">Email verified</span>
              {user.email_verified ? 'Yes' : 'No'}
            </span>
            <span className="flex items-center gap-1.5">
              <span className="text-muted-foreground">Invitation</span>
              {user.invitation ? (
                <>
                  <StatusPill status={user.invitation.status} />
                  {user.invitation.status === 'pending' && (
                    <span className="text-muted-foreground text-xs">
                      expires{' '}
                      <time dateTime={user.invitation.expires_at}>
                        {new Date(user.invitation.expires_at).toLocaleDateString()}
                      </time>
                    </span>
                  )}
                </>
              ) : (
                <span className="text-muted-foreground">none</span>
              )}
            </span>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>Roles</Label>
          <RolesPicker selected={roleIds} onChange={setRoleIds} />
        </div>
        <div className="flex justify-between pt-2">
          <Button variant="outline" onClick={() => setDeleteOpen(true)}>
            <Trash2 className="size-4" /> Delete
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onDone}>
              Cancel
            </Button>
            <Button
              disabled={update.isPending}
              onClick={() =>
                update.mutate(
                  {
                    first_name: firstName || null,
                    last_name: lastName || null,
                    role_ids: roleIds,
                  },
                  { onSuccess: onDone },
                )
              }
            >
              {update.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </div>
      </CardContent>

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete user"
        description={`Delete ${user.email}? They lose access immediately and their external login links are dropped. You can restore the account for a limited time afterwards.`}
        confirmLabel="Delete"
        destructive
        pending={del.isPending}
        onConfirm={() => del.mutate(user.id, { onSuccess: onDone })}
      />
    </Card>
  )
}
