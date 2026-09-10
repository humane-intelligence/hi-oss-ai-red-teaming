import { useEffect, useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Trash2 } from 'lucide-react'
import { useAllPermissions, useRole } from './queries'
import { useCreateRole, useDeleteRole, useUpdateRole } from './mutations'
import { applyApiError } from '@/lib/api/form'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent } from '@/components/ui/card'
import type { RoleResponse, RoleUpdate } from '@/lib/api/types'

// A role is only assignable inside a group if it grants this (the object type's
// `required_permission`), so the toggle stays disabled until the permission is picked.
const GROUP_READ = 'evaluation_groups:read'

const schema = z
  .object({
    name: z
      .string()
      .max(64, 'Max 64 characters')
      .regex(
        /^[a-z][a-z0-9_-]*$/,
        'Lowercase letter first, then lowercase letters, digits, _ or -',
      ),
    display_name: z.string().min(1, 'Required').max(128, 'Max 128 characters'),
    description: z.string().max(255, 'Max 255 characters'),
    permissions: z.array(z.string()),
    is_active: z.boolean(),
    is_default: z.boolean(),
    is_participant_default: z.boolean(),
    is_object_assignable: z.boolean(),
    // Not an input — mirrored into form state so the refinement below can exempt system
    // roles, whose permissions and object-assignability are code-owned and uneditable.
    is_system: z.boolean(),
  })
  .superRefine((v, ctx) => {
    if (v.is_system) return
    // Mirrors `_assert_grantable_in_group`: dropping the permission while the role stays
    // in-group assignable is a guaranteed 409, so block it at the field that caused it.
    if (v.is_object_assignable && !v.permissions.includes(GROUP_READ)) {
      ctx.addIssue({
        code: 'custom',
        path: ['permissions'],
        message: `An in-group assignable role must grant ${GROUP_READ}`,
      })
    }
  })
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = {
  name: '',
  display_name: '',
  description: '',
  permissions: [],
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
  is_system: false,
}

const toFormValues = (role: RoleResponse): FormValues => ({
  name: role.name,
  display_name: role.display_name,
  description: role.description ?? '',
  permissions: role.permissions ?? [],
  is_active: role.is_active,
  is_default: role.is_default,
  is_participant_default: role.is_participant_default,
  is_object_assignable: role.is_object_assignable,
  is_system: role.is_system,
})

export function RoleFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const existing = useRole(id ?? '')
  const role = existing.data
  const permissions = useAllPermissions()
  const create = useCreateRole()
  const update = useUpdateRole(id ?? '')
  const remove = useDeleteRole()
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { register, handleSubmit, reset, setError, setValue, control, formState } =
    useForm<FormValues>({
      resolver: zodResolver(schema),
      defaultValues: EMPTY,
    })

  useEffect(() => {
    if (!role) return
    reset(toFormValues(role))
  }, [role, reset])

  useUnsavedGuard(formState.isDirty)

  const isSystem = Boolean(role?.is_system)
  const editable = !isSystem // name/label/description/permissions; false for system roles
  const selectedPermissions = useWatch({ control, name: 'permissions' })
  const isActive = useWatch({ control, name: 'is_active' })
  const isObjectAssignable = useWatch({ control, name: 'is_object_assignable' })
  const grantsGroupRead = selectedPermissions.includes(GROUP_READ)
  // What in-group assignment accepts, mirrored client-side so the operator sees the
  // precondition instead of a 409 (`_assert_grantable_in_group` on the backend).
  const grantableInGroup = isObjectAssignable && grantsGroupRead

  // The catalog marks which permissions a custom role may carry; hide the rest so the
  // choice can't be made (the backend 400 stays the backstop).
  const permissionOptions = (permissions.data?.items ?? []).filter((p) => p.is_delegable)

  const togglePermission = (key: string) => {
    const next = selectedPermissions.includes(key)
      ? selectedPermissions.filter((k) => k !== key)
      : [...selectedPermissions, key]
    setValue('permissions', next, { shouldDirty: true })
  }

  const onSubmit = handleSubmit(async (v) => {
    try {
      if (!isEdit) {
        const created = await create.mutateAsync({
          name: v.name.trim(),
          display_name: v.display_name.trim(),
          description: v.description.trim() || null,
          permissions: v.permissions,
        })
        navigate(`/roles/${created.id}/edit`)
        return
      }
      const body: RoleUpdate = {}
      if (editable) {
        body.display_name = v.display_name.trim()
        // Empty string, not null: the API reads null as "leave unchanged", so `null`
        // would make clearing the description a silent no-op.
        body.description = v.description.trim()
        body.permissions = v.permissions
        // A real two-way toggle, unlike the defaults — send it only when it moved.
        if (role && v.is_object_assignable !== role.is_object_assignable)
          body.is_object_assignable = v.is_object_assignable
      }
      if (role && v.is_active !== role.is_active) body.is_active = v.is_active
      // Never clears a default directly (a 409) — only sets a new one, and only if it stays active.
      if (role && v.is_active && v.is_default && !role.is_default) body.is_default = true
      if (role && v.is_active && v.is_participant_default && !role.is_participant_default)
        body.is_participant_default = true
      await update.mutateAsync(body)
      navigate('/roles')
    } catch (err) {
      // A guard rejection (409) carries no field errors, so nothing would correct the form —
      // refetch so the checkboxes match the server instead of showing the rejected value.
      // Reset from the result, not via the `role` effect: a rejected write leaves the payload
      // identical, so structural sharing keeps the same reference and the effect never fires.
      if (!applyApiError(err, setError)) {
        const { data } = await existing.refetch()
        if (data) reset(toFormValues(data))
      }
    }
  })

  if (isEdit && existing.isPending) {
    return (
      <div className="p-6">
        <DetailSkeleton />
      </div>
    )
  }
  if (isEdit && !role) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/roles')}>
          <ArrowLeft className="size-4" /> Back
        </Button>
        <p className="text-muted-foreground">Role not found.</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/roles')}>
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title={isEdit ? role!.display_name : 'New role'}
        breadcrumbs={
          <Breadcrumbs
            items={[{ label: 'Roles', to: '/roles' }, { label: isEdit ? 'Edit' : 'New' }]}
          />
        }
      />
      {isSystem && (
        <p className="bg-muted/40 text-muted-foreground rounded-md border px-3 py-2 text-sm">
          This is a platform role. Its label and permissions are code-managed; only activation and
          the default flag can change here.
        </p>
      )}
      <Card>
        <CardContent className="space-y-4 pt-6">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <FormField
              label="Name"
              htmlFor="name"
              error={formState.errors.name?.message}
              hint={
                isEdit
                  ? 'The slug is immutable after creation.'
                  : 'Stable machine identifier; cannot be changed later.'
              }
            >
              {isEdit ? (
                <Input id="name" value={role!.name} disabled readOnly />
              ) : (
                <Input id="name" placeholder="lead_reviewer" {...register('name')} />
              )}
            </FormField>
            <FormField
              label="Display name"
              htmlFor="display_name"
              error={formState.errors.display_name?.message}
            >
              {editable ? (
                <Input id="display_name" {...register('display_name')} />
              ) : (
                <Input id="display_name" value={role!.display_name} disabled readOnly />
              )}
            </FormField>
            <FormField
              label="Description (optional)"
              htmlFor="description"
              error={formState.errors.description?.message}
            >
              {editable ? (
                <Textarea id="description" rows={3} {...register('description')} />
              ) : (
                <Textarea
                  id="description"
                  value={role!.description ?? ''}
                  disabled
                  readOnly
                  rows={3}
                />
              )}
            </FormField>

            <FormField
              label="Permissions"
              htmlFor="permissions"
              error={formState.errors.permissions?.message}
            >
              {editable ? (
                <div
                  id="permissions"
                  className="max-h-72 space-y-2 overflow-y-auto rounded-md border p-3"
                >
                  {permissions.isPending && (
                    <p className="text-muted-foreground text-xs">Loading permissions…</p>
                  )}
                  {permissionOptions.map((p) => (
                    <label key={p.key} className="flex items-start gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="mt-0.5 size-4 rounded border"
                        checked={selectedPermissions.includes(p.key)}
                        onChange={() => togglePermission(p.key)}
                      />
                      <span>
                        <span className="font-mono text-xs">{p.key}</span>
                        <span className="text-muted-foreground block">{p.description}</span>
                      </span>
                    </label>
                  ))}
                </div>
              ) : (
                <div className="flex flex-wrap gap-1">
                  {(role!.permissions ?? []).map((p) => (
                    <Badge key={p} variant="secondary" className="font-mono">
                      {p}
                    </Badge>
                  ))}
                </div>
              )}
            </FormField>

            {isEdit && (
              <div className="space-y-3 rounded-md border p-3">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="size-4 rounded border"
                    disabled={role!.is_default || role!.is_participant_default}
                    {...register('is_active')}
                  />
                  Active
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="size-4 rounded border"
                    disabled={role!.is_default || !isActive}
                    {...register('is_default')}
                  />
                  Default role for new users
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="size-4 rounded border"
                    // Self-join grants this as an in-group role, so the backend requires it
                    // to be group-assignable first — same precondition as the toggle below.
                    disabled={role!.is_participant_default || !isActive || !grantableInGroup}
                    {...register('is_participant_default')}
                  />
                  Default role for group self-join
                </label>
                {isEdit && !role!.is_participant_default && !grantableInGroup && (
                  <p className="text-muted-foreground text-xs">
                    A self-join default must be assignable inside groups and grant{' '}
                    <span className="font-mono">{GROUP_READ}</span>.
                  </p>
                )}
                {editable && (
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="size-4 rounded border"
                      disabled={
                        role!.is_participant_default || (!isObjectAssignable && !grantsGroupRead)
                      }
                      {...register('is_object_assignable')}
                    />
                    Assignable inside evaluation groups
                  </label>
                )}
                {editable && !grantsGroupRead && (
                  <p className="text-muted-foreground text-xs">
                    Add <span className="font-mono">{GROUP_READ}</span> to make this role assignable
                    inside a group.
                  </p>
                )}
                <p className="text-muted-foreground text-xs">
                  {role!.is_default || role!.is_participant_default
                    ? 'Reassign a default by making another active role the default; a default role cannot be deactivated.'
                    : 'Deactivating signs out everyone whose access depends on this role; protected and default roles cannot be deactivated.'}
                </p>
              </div>
            )}

            <div className="flex items-center justify-between pt-2">
              {isEdit && editable ? (
                <Button
                  type="button"
                  variant="outline"
                  className="text-destructive"
                  onClick={() => setConfirmDelete(true)}
                >
                  <Trash2 className="size-4" /> Delete
                </Button>
              ) : (
                <span />
              )}
              <div className="flex gap-2">
                <Button type="button" variant="outline" onClick={() => navigate('/roles')}>
                  Cancel
                </Button>
                <Button type="submit" disabled={formState.isSubmitting}>
                  {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Create'}
                </Button>
              </div>
            </div>
          </form>
        </CardContent>
      </Card>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete role"
        description={
          role
            ? `Delete ${role.display_name}? Anyone still holding it loses its permissions; the default and sole-role guards may block this. ${REVERSIBLE_DELETE_NOTE}`
            : ''
        }
        confirmLabel="Delete"
        destructive
        pending={remove.isPending}
        onConfirm={() => {
          if (!id) return
          remove.mutate(id, { onSuccess: () => navigate('/roles') })
        }}
      />
    </div>
  )
}
