import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { ArrowLeft, Copy, Download, Pencil, Plus } from 'lucide-react'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { StatTile } from '@/components/shared/stat-tile'
import {
  useEvaluationGroup,
  useGroupMembers,
  useGroupAnnotators,
  useEvaluationGroupMetrics,
} from './queries'
import {
  useApproveGroup,
  useDuplicateGroup,
  useFinishGroup,
  useJoinGroup,
  usePublishGroup,
  useRejectGroup,
  useRequestChangesGroup,
  useSubmitGroup,
} from './mutations'
import { useUserLookup } from '@/features/users/queries'
import { LicenseValue } from '@/features/licenses/license-value'
import { humanizeError } from '@/lib/api/problem'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { GroupIdentityHeading } from './group-trail'
import { PublicationStatusBadge } from './status-badge'
import { EvaluationStatusBadge } from '@/features/evaluations/status-badge'
import { Field } from '@/components/shared/field'
import { DataTable, type Column } from '@/components/shared/data-table'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { PageActions } from '@/components/shared/page-actions'
import { Badge } from '@/components/ui/badge'
import { MembersSection } from './members-section'
import { GroupMetricsTab } from './group-metrics-tab'
import { METRICS_ACCESS_LABELS } from './metrics-access'
import { ExportDialog } from '@/features/exports/export-dialog'
import { EXPORT_AUTHORITY_PERMISSIONS } from '@/features/exports/authority'
import { ExportsSection } from '@/features/exports/exports-section'
import { Tabs, type TabItem } from '@/components/ui/tabs'
import { tabPanelProps } from '@/components/ui/tab-panel'
import { useTabParam } from '@/components/shared/use-tab-param'
import { usePermissions, useObjectPermissions } from '@/lib/auth/use-permissions'
import { evaluationsBlockedHint, groupAcceptsEvaluations } from './lifecycle'
import { useAuth } from '@/lib/auth/auth-context'
import type { EvaluationResponse } from '@/lib/api/types'

// Every tab this page can address, whatever the caller's authority — see `useTabParam`.
const TAB_VOCABULARY = ['overview', 'metrics', 'exports']

const childColumns: Column<EvaluationResponse>[] = [
  { header: 'Title', cell: (e) => <span className="font-medium">{e.title}</span> },
  { header: 'Status', cell: (e) => <EvaluationStatusBadge status={e.status} /> },
  { header: 'Models', cell: (e) => e.models?.length ?? 0 },
]

export function EvaluationGroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const query = useEvaluationGroup(id ?? '')
  const group = query.data
  const children = group?.evaluations ?? []
  // Gate group actions on the caller's effective in-group authority (mirrors the
  // server's per-object check), not the global permission union.
  const groupPerms = useObjectPermissions(group?.user_permissions)
  // Duplicate creates a *new* group, so it gates on the global create capability
  // rather than this group's per-object authority.
  const { has } = usePermissions()
  const canManageMembers = groupPerms.hasAny([
    'evaluation_groups:manage',
    'evaluation_groups:manage_members',
  ])
  // Whole-event metrics are gated server-side by the group's configured
  // `metrics_access_during` / `metrics_access_after` levels (the in-group `owner`
  // and `evaluation_groups:manage` break-glass always get the full aggregate; a
  // `members_personal_metrics` member gets a personal slice). The server folds that
  // decision into the group detail's `user_permissions` — injecting
  // `evaluation_groups:view_metrics` exactly when the caller would be admitted (any
  // scope) — so gating the inline metric sections on that key keeps them in step with
  // the endpoint and a non-admitted viewer never 403s (and toasts) on the fetch. The
  // metrics response's `scope` then drives the personal-view badge.
  const canViewMetrics = groupPerms.has('evaluation_groups:view_metrics')
  const metrics = useEvaluationGroupMetrics(id ?? '', { enabled: canViewMetrics })
  const metricsData = metrics.data
  const members = useGroupMembers(id ?? '')
  // The annotator list is manage-members-gated server-side; only fetch it when the
  // caller can manage, so a plain member doesn't 403 (and toast) on it.
  const annotators = useGroupAnnotators(id ?? '', { enabled: canManageMembers })

  const submit = useSubmitGroup(id ?? '')
  const publish = usePublishGroup(id ?? '')
  const finish = useFinishGroup(id ?? '')
  const join = useJoinGroup(id ?? '')
  const approve = useApproveGroup(id ?? '')
  const requestChanges = useRequestChangesGroup(id ?? '')
  const reject = useRejectGroup(id ?? '')
  const duplicate = useDuplicateGroup()
  const [rejectOpen, setRejectOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  // Export gates on the same in-group owner / admin authority the server enforces
  // (`assert_export_authority` → `evaluation_groups:update`, `:manage` break-glass),
  // not on a global read permission.
  const canExport = groupPerms.hasAny(EXPORT_AUTHORITY_PERMISSIONS)
  // No layout space is reserved while either authority resolves: holding it would collapse and
  // shift the body for the common viewer, who gets one tab and therefore no bar at all. An
  // owner's one-time pop-in is the lesser evil.
  //
  // The URL is validated against the full vocabulary, not the visible subset: authority arrives
  // with the async group fetch, so validating against what is visible on the first render would
  // strip a legitimate `?tab=exports` before the permission lands. Which tab actually *renders*
  // is then the visible-set decision below.
  const tabItems: TabItem[] = [
    { value: 'overview', label: 'Overview' },
    ...(canViewMetrics ? [{ value: 'metrics', label: 'Metrics' }] : []),
    ...(canExport ? [{ value: 'exports', label: 'Exports' }] : []),
  ]
  const [tab, setTab] = useTabParam(TAB_VOCABULARY, 'overview')
  const activeTab = tabItems.some((item) => item.value === tab) ? tab : 'overview'
  // No tablist means nothing for `aria-labelledby` to point at.
  const panel = tabItems.length > 1 ? tabPanelProps : () => ({})
  const [duplicateOpen, setDuplicateOpen] = useState(false)
  const [includeChildren, setIncludeChildren] = useState(false)
  const [requestChangesOpen, setRequestChangesOpen] = useState(false)
  const { user } = useAuth()
  const lookup = useUserLookup()
  // What the group's next lifecycle step would refuse, straight from the server's own
  // gate (empty = ready) — gates that step's button and drives the inline hint below
  // the header. Non-empty only where a step is pending: submit while the group is a
  // draft, publish once it is approved.
  const blockers = group?.publication_blockers ?? []
  const isSubmittableState = group?.status === 'draft' || group?.status === 'changes_requested'

  // Hide Join once the caller already holds a role here (owner included) — the
  // members list is readable by anyone who can see the group, so this is reliable
  // for every viewer, not just managers.
  const alreadyMember = members.data?.items.some((m) => m.user.id === user?.id) ?? false
  // Self-joinable = public or organization (the detail page only loads groups the caller can see,
  // so an org group here is one of theirs); invitation-only comes by invite, never self-join.
  const canJoin =
    group?.status === 'published' && group.access_level !== 'invitation_only' && !alreadyMember

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => navigate('/evaluation-groups')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>

      {query.isPending && <DetailSkeleton />}
      {query.isError && <p className="text-destructive">{humanizeError(query.error)}</p>}

      {group && (
        <>
          <header className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                {/* Above the badge row, not inside it: the badges align to the heading they qualify,
                    and a two-row block in a centred flex row would pull them up beside the crumb. */}
                <Breadcrumbs
                  items={[{ label: 'Evaluation Groups', to: '/evaluation-groups' }]}
                  label="Evaluation groups list"
                />
                <div className="flex flex-wrap items-center gap-3">
                  <GroupIdentityHeading groupId={group.id} />
                  <PublicationStatusBadge status={group.status} />
                  <Badge variant="outline">{group.access_level.replace(/_/g, ' ')}</Badge>
                </div>
                <p className="text-muted-foreground font-mono text-xs">
                  by <span>{lookup(group.created_by_id)}</span>
                </p>
                {group.description && (
                  <p className="text-muted-foreground max-w-2xl">{group.description}</p>
                )}
              </div>
              <PageActions
                primary={
                  <>
                    {groupPerms.has('evaluation_groups:update') && isSubmittableState && (
                      <Button
                        size="sm"
                        disabled={submit.isPending || blockers.length > 0}
                        title={blockers.length > 0 ? blockers.join(' ') : undefined}
                        onClick={() => submit.mutate()}
                      >
                        Submit for approval
                      </Button>
                    )}
                    {groupPerms.has('evaluation_groups:update') &&
                      group.status === 'pending_approval' && (
                        <Button
                          size="sm"
                          disabled={approve.isPending}
                          onClick={() => approve.mutate()}
                        >
                          Approve
                        </Button>
                      )}
                    {groupPerms.has('evaluation_groups:update') && group.status === 'approved' && (
                      <Button
                        size="sm"
                        disabled={publish.isPending || blockers.length > 0}
                        title={blockers.length > 0 ? blockers.join(' ') : undefined}
                        onClick={() => publish.mutate()}
                      >
                        Publish
                      </Button>
                    )}
                    {groupPerms.has('evaluation_groups:update') && group.status === 'published' && (
                      <Button size="sm" disabled={finish.isPending} onClick={() => finish.mutate()}>
                        Finish
                      </Button>
                    )}
                    {/* Primary for a non-member: the rest of `primary` needs
                        `evaluation_groups:update`, so without this a plain viewer faces an empty
                        slot beside a kebab. A non-member who does hold that permission sees Finish
                        and Join together, which is correct - they are different actions. */}
                    {canJoin && (
                      <Button size="sm" disabled={join.isPending} onClick={() => join.mutate()}>
                        Join
                      </Button>
                    )}
                  </>
                }
                secondary={[
                  {
                    key: 'request-changes',
                    label: 'Request changes',
                    onSelect: () => setRequestChangesOpen(true),
                    disabled: requestChanges.isPending,
                    when:
                      groupPerms.has('evaluation_groups:manage') &&
                      group.status === 'pending_approval',
                  },
                  {
                    key: 'reject',
                    label: 'Reject',
                    onSelect: () => setRejectOpen(true),
                    destructive: true,
                    when:
                      groupPerms.has('evaluation_groups:manage') &&
                      group.status === 'pending_approval',
                  },
                  {
                    key: 'duplicate',
                    label: 'Duplicate',
                    icon: Copy,
                    onSelect: () => {
                      setIncludeChildren(false)
                      setDuplicateOpen(true)
                    },
                    disabled: duplicate.isPending,
                    when: has('evaluation_groups:create'),
                  },
                  {
                    key: 'edit',
                    label: 'Edit',
                    icon: Pencil,
                    onSelect: () => navigate(`/evaluation-groups/${group.id}/edit`),
                    when: groupPerms.has('evaluation_groups:update'),
                  },
                  {
                    key: 'export',
                    label: 'Export',
                    icon: Download,
                    onSelect: () => setExportOpen(true),
                    when: canExport,
                  },
                ]}
              />
            </div>

            {group.rejection_reason && (
              <div className="border-destructive/40 bg-destructive/10 rounded-md border px-4 py-3 text-sm">
                <span className="text-destructive font-medium">Rejected</span> —{' '}
                {group.rejection_reason}
              </div>
            )}

            {groupPerms.has('evaluation_groups:update') && blockers.length > 0 && (
              <div className="bg-muted/50 text-muted-foreground rounded-md border px-4 py-3 text-sm">
                <span className="text-foreground font-medium">
                  {isSubmittableState ? 'Not ready to submit.' : 'Not ready to publish.'}
                </span>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {blockers.map((blocker) => (
                    <li key={blocker}>{blocker}</li>
                  ))}
                </ul>
                {/* The group form only fixes the submit gate's gaps — the publish gate wants
                    evaluations and scenarios, which live under the children below. */}
                {isSubmittableState && (
                  <button
                    type="button"
                    className="text-foreground mt-2 font-medium underline underline-offset-2"
                    onClick={() => navigate(`/evaluation-groups/${group.id}/edit`)}
                  >
                    Edit draft
                  </button>
                )}
              </div>
            )}

            <div className={cn('grid grid-cols-2 gap-3', canManageMembers && 'sm:grid-cols-3')}>
              <StatTile label="Evaluations" value={children.length} />
              <StatTile label="Members" value={members.data?.items.length ?? 0} />
              {canManageMembers && (
                <StatTile label="Annotators" value={annotators.data?.items.length ?? 0} />
              )}
            </div>
          </header>

          {tabItems.length > 1 && <Tabs tabs={tabItems} value={activeTab} onChange={setTab} />}

          {activeTab === 'overview' && (
            <div
              {...panel('overview')}
              className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-3"
            >
              <div className="min-w-0 space-y-6 lg:col-span-2">
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <h2 className="font-display text-lg font-semibold tracking-tight">
                      Evaluations ({children.length})
                    </h2>
                    {groupPerms.has('evaluations:create') &&
                      groupAcceptsEvaluations(group.status) && (
                        <Button
                          size="sm"
                          onClick={() => navigate(`/evaluations/new?group=${group.id}`)}
                        >
                          <Plus className="size-4" /> New evaluation
                        </Button>
                      )}
                  </div>
                  <DataTable
                    columns={childColumns}
                    rows={children}
                    rowKey={(e) => e.id}
                    onRowClick={(e) => navigate(`/evaluations/${e.id}`)}
                    emptyLabel="No evaluations yet."
                    emptyHint={
                      groupAcceptsEvaluations(group.status)
                        ? 'Evaluations added to this group show up here.'
                        : evaluationsBlockedHint(group.status)
                    }
                  />
                </div>

                <MembersSection
                  groupId={group.id}
                  accessLevel={group.access_level}
                  userPermissions={group.user_permissions ?? []}
                />
              </div>

              <aside className="min-w-0 space-y-6">
                <Card>
                  <CardHeader>
                    <CardTitle>Details</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <dl className="space-y-3">
                      <Field label="Data license">
                        <LicenseValue license={group.effective_license} />
                        {!group.data_license_id && (
                          <span className="text-muted-foreground ml-1.5 text-xs">
                            (platform default)
                          </span>
                        )}
                      </Field>
                      <Field label="Start date">{group.start_date ?? '—'}</Field>
                      <Field label="End date">{group.end_date ?? '—'}</Field>
                      <Field label="Analytics (while active)">
                        {METRICS_ACCESS_LABELS[group.metrics_access_during]}
                      </Field>
                      <Field label="Analytics (after finish)">
                        {METRICS_ACCESS_LABELS[group.metrics_access_after]}
                      </Field>
                      <Field label="Created">{new Date(group.created_at).toLocaleString()}</Field>
                    </dl>
                  </CardContent>
                </Card>
              </aside>
            </div>
          )}

          {activeTab === 'metrics' && (
            <div {...panel('metrics')}>
              <GroupMetricsTab data={metricsData} isError={metrics.isError} error={metrics.error} />
            </div>
          )}

          {activeTab === 'exports' && (
            <div {...panel('exports')} className="bg-card rounded-lg border p-4">
              <ExportsSection scope={{ evaluation_group_id: group.id }} />
            </div>
          )}

          <ConfirmDialog
            open={rejectOpen}
            onOpenChange={setRejectOpen}
            title="Reject group"
            description="Record why this group is being rejected."
            confirmLabel="Reject"
            destructive
            pending={reject.isPending}
            reason={{
              label: 'Rejection reason',
              required: true,
              placeholder: 'Why is this being rejected?',
            }}
            onConfirm={(reason) => reject.mutate(reason, { onSuccess: () => setRejectOpen(false) })}
          />

          <ConfirmDialog
            open={duplicateOpen}
            onOpenChange={setDuplicateOpen}
            title="Duplicate group"
            description={
              <div className="space-y-3">
                <p>This creates a new draft you own, copied from this group.</p>
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5 size-4 rounded border"
                    checked={includeChildren}
                    onChange={(e) => setIncludeChildren(e.target.checked)}
                  />
                  <span>Also copy its evaluations, scenarios, tasks, and model assignments.</span>
                </label>
              </div>
            }
            confirmLabel="Duplicate"
            pending={duplicate.isPending}
            onConfirm={() =>
              duplicate.mutate(
                { sourceId: group.id, includeChildren },
                {
                  onSuccess: (created) => {
                    setDuplicateOpen(false)
                    navigate(`/evaluation-groups/${created.id}/edit`)
                  },
                },
              )
            }
          />

          <ExportDialog
            open={exportOpen}
            onOpenChange={setExportOpen}
            scope={{ evaluation_group_id: group.id }}
            title="Export evaluation group"
          />

          <ConfirmDialog
            open={requestChangesOpen}
            onOpenChange={setRequestChangesOpen}
            title="Request changes"
            description="Bounce this group back to its owner for edits? It returns to changes-requested and must be re-submitted before it can be approved."
            confirmLabel="Request changes"
            pending={requestChanges.isPending}
            onConfirm={() =>
              requestChanges.mutate(undefined, { onSuccess: () => setRequestChangesOpen(false) })
            }
          />
        </>
      )}
    </div>
  )
}
