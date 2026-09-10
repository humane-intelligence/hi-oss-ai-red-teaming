import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ExternalLink, Pencil, Trash2 } from 'lucide-react'
import { useLicense } from './queries'
import { useDeleteLicense } from './mutations'
import { LicenseTextEditor } from './license-text-editor'
import { isHttpUrl } from './url'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { StatTile } from '@/components/shared/stat-tile'
import { Field } from '@/components/shared/field'
import { InfoHint } from '@/components/shared/info-hint'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { humanizeError } from '@/lib/api/problem'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useAuth } from '@/lib/auth/auth-context'

export function LicenseDetailPage() {
  const { id } = useParams<{ id: string }>()
  const licenseId = id ?? ''
  const navigate = useNavigate()
  const { has } = usePermissions()
  const { user } = useAuth()
  const query = useLicense(licenseId)
  const license = query.data
  const del = useDeleteLicense()
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [textOpen, setTextOpen] = useState(false)

  // The read serves tombstones so a deleted licence's text stays legible for the groups that still
  // reference it; every write path resolves live rows only, so a tombstone offers no actions.
  const isDeleted = license?.deleted_at != null
  // Mirror the backend gate (`_assert_may_update_license`): a curated licence's metadata is managed in
  // code and never mutable via the API; a user-authored one is editable by its author, or a
  // `licenses:manage` holder. The one exception is a curated licence's text — see `canEditCuratedText`.
  const mayManage =
    license !== undefined &&
    !isDeleted &&
    !license.is_curated &&
    (has('licenses:manage') || license.created_by_id === user?.id)
  const canEdit = mayManage && has('licenses:update')
  const canDelete = mayManage && has('licenses:delete')
  // `content` is the single field the API takes on a curated row, and only where the catalog ships
  // none — for a row whose text ships in code the resync would revert the edit, so the API refuses it
  // (403). `text_managed_in_code` is that same predicate, projected, so this cannot drift from the
  // gate when another catalog entry starts shipping a text.
  const canEditCuratedText =
    license !== undefined &&
    !isDeleted &&
    license.is_curated &&
    !license.text_managed_in_code &&
    has('licenses:update') &&
    has('licenses:manage')

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/licenses')}>
        <ArrowLeft className="size-4" /> Back
      </Button>

      {query.isPending && <DetailSkeleton />}
      {query.isError && <p className="text-destructive">{humanizeError(query.error)}</p>}

      {license && (
        <>
          <Breadcrumbs
            items={[{ label: 'Data licenses', to: '/licenses' }, { label: license.name }]}
          />

          <header className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="font-display text-2xl font-semibold tracking-tight">
                    {license.name}
                  </h1>
                  {isDeleted && <Badge variant="err">deleted</Badge>}
                </div>
                <p className="text-muted-foreground max-w-2xl">
                  {license.short_description || '—'}
                </p>
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                {canEdit && (
                  <Button size="sm" onClick={() => navigate(`/licenses/${license.id}/edit`)}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                )}
                {canDelete && (
                  <Button variant="outline" size="sm" onClick={() => setDeleteOpen(true)}>
                    <Trash2 className="size-4" /> Delete
                  </Button>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <StatTile label="Type" value={license.is_curated ? 'Curated' : 'Custom'} />
              <StatTile label="SPDX" value={license.spdx_id ?? '—'} />
              <StatTile label="Version" value={license.version ?? '—'} />
            </div>
          </header>

          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="space-y-3">
                <Field label="Platform default">
                  {license.is_default ? <Badge>Default</Badge> : 'No'}
                </Field>
                <Field label="Conversation data">
                  {license.protects_conversation_data ? (
                    <span className="flex items-center gap-1">
                      <Badge variant="ok">protected</Badge>
                      <InfoHint text="Message text is stored encrypted at rest for conversations started under this license. Titles, tags and attachments are not, and conversations already running are not covered — including messages added to them later." />
                    </span>
                  ) : (
                    'Not protected'
                  )}
                </Field>
                <Field label="Reference">
                  {isHttpUrl(license.reference_url) ? (
                    <a
                      href={license.reference_url}
                      target="_blank"
                      rel="noreferrer"
                      // `wrap-anywhere`: a licence URL is one unbroken token with no break point.
                      className="hover:text-foreground inline-flex min-w-0 items-center gap-1 wrap-anywhere underline-offset-2 hover:underline"
                    >
                      {license.reference_url} <ExternalLink className="size-3.5" />
                    </a>
                  ) : (
                    // Non-http(s) values render as plain text (never a clickable href) — see isHttpUrl.
                    license.reference_url || '—'
                  )}
                </Field>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-4">
              <CardTitle>License text</CardTitle>
              {canEditCuratedText && (
                <Button variant="outline" size="sm" onClick={() => setTextOpen(true)}>
                  <Pencil className="size-4" />
                  {license.content ? 'Edit license text' : 'Add license text'}
                </Button>
              )}
            </CardHeader>
            <CardContent>
              {license.content ? (
                <pre className="bg-muted max-h-[32rem] overflow-auto rounded-md p-4 text-sm whitespace-pre-wrap">
                  {license.content}
                </pre>
              ) : (
                <p className="text-muted-foreground">No license text on record yet.</p>
              )}
            </CardContent>
          </Card>

          <LicenseTextEditor
            licenseId={licenseId}
            licenseName={license.name}
            current={license.content}
            open={textOpen}
            onOpenChange={setTextOpen}
          />

          <ConfirmDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            title="Delete license"
            description={`Delete “${license.name}”? Groups and evaluations already referencing it keep resolving it.`}
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() => del.mutate(license.id, { onSuccess: () => navigate('/licenses') })}
          />
        </>
      )}
    </div>
  )
}
