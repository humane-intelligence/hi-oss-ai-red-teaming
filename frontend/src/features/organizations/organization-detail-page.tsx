import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Pencil, Trash2 } from 'lucide-react'
import { useOrganization, useOrganizationMembers } from './queries'
import { useDeleteOrganization } from './mutations'
import { MembersSection } from './members-section'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { StatTile } from '@/components/shared/stat-tile'
import { Field } from '@/components/shared/field'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { humanizeError } from '@/lib/api/problem'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/lib/auth/use-permissions'

export function OrganizationDetailPage() {
  const { id } = useParams<{ id: string }>()
  const organizationId = id ?? ''
  const navigate = useNavigate()
  const { has } = usePermissions()
  const query = useOrganization(organizationId)
  const organization = query.data
  const members = useOrganizationMembers(organizationId)
  const del = useDeleteOrganization()
  const [deleteOpen, setDeleteOpen] = useState(false)

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => navigate('/organizations')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>

      {query.isPending && <DetailSkeleton />}
      {query.isError && <p className="text-destructive">{humanizeError(query.error)}</p>}

      {organization && (
        <>
          <Breadcrumbs
            items={[{ label: 'Organizations', to: '/organizations' }, { label: organization.name }]}
          />

          <header className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                <h1 className="font-display text-2xl font-semibold tracking-tight">
                  {organization.name}
                </h1>
                {organization.description && (
                  <p className="text-muted-foreground max-w-2xl">{organization.description}</p>
                )}
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                {has('organizations:update') && (
                  <Button
                    size="sm"
                    onClick={() => navigate(`/organizations/${organization.id}/edit`)}
                  >
                    <Pencil className="size-4" /> Edit
                  </Button>
                )}
                {has('organizations:delete') && (
                  <Button variant="outline" size="sm" onClick={() => setDeleteOpen(true)}>
                    <Trash2 className="size-4" /> Delete
                  </Button>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <StatTile label="Members" value={members.data?.total ?? 0} />
              <StatTile
                label="Created"
                value={new Date(organization.created_at).toLocaleDateString()}
              />
              <StatTile
                label="Updated"
                value={new Date(organization.updated_at).toLocaleDateString()}
              />
            </div>
          </header>

          <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-3">
            <div className="min-w-0 lg:col-span-2">
              <MembersSection organizationId={organization.id} />
            </div>

            <aside className="min-w-0">
              <Card>
                <CardHeader>
                  <CardTitle>Details</CardTitle>
                </CardHeader>
                <CardContent>
                  <dl className="space-y-3">
                    <Field label="Description">{organization.description || '—'}</Field>
                    <Field label="Created">
                      {new Date(organization.created_at).toLocaleString()}
                    </Field>
                    <Field label="Updated">
                      {new Date(organization.updated_at).toLocaleString()}
                    </Field>
                  </dl>
                </CardContent>
              </Card>
            </aside>
          </div>

          <ConfirmDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            title="Delete organization"
            description={`Delete “${organization.name}”? Its members lose their organization until it is restored, and a live evaluation group still referencing it blocks the delete. ${REVERSIBLE_DELETE_NOTE}`}
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() =>
              del.mutate(organization.id, { onSuccess: () => navigate('/organizations') })
            }
          />
        </>
      )}
    </div>
  )
}
