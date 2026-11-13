import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Copy,
  Download,
  MessageSquarePlus,
  Pencil,
  Plus,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react'
import { useEvaluation, useEvaluationMetrics } from './queries'
import {
  useApproveEvaluation,
  useDuplicateEvaluation,
  useRejectEvaluation,
  useUnassignModel,
} from './mutations'
import { EvaluationStatusBadge } from './status-badge'
import { EvaluationMetricsPanel } from './metrics-section'
import { AssignModelDialog } from './assign-model-dialog'
import { AllowedTagsCard } from './allowed-tags-card'
import { ScenariosSection } from '@/features/scenarios/scenarios-section'
import { useEvaluationScenarios, useTaskCount } from '@/features/scenarios/queries'
import { ConversationsSection } from '@/features/conversations/conversations-section'
import { StartConversationDialog } from '@/features/conversations/start-conversation-dialog'
import { useEvaluationGroup } from '@/features/evaluation-groups/queries'
import { GroupIdentity } from '@/features/evaluation-groups/group-trail'
import { groupAcceptsEvaluations } from '@/features/evaluation-groups/lifecycle'
import { LicenseValue } from '@/features/licenses/license-value'
import { humanizeError } from '@/lib/api/problem'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { Field } from '@/components/shared/field'
import { StatTile } from '@/components/shared/stat-tile'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { ExportDialog } from '@/features/exports/export-dialog'
import { EXPORT_AUTHORITY_PERMISSIONS } from '@/features/exports/authority'
import { ExportsSection } from '@/features/exports/exports-section'
import { Tabs, type TabItem } from '@/components/ui/tabs'
import { tabPanelProps } from '@/components/ui/tab-panel'
import { useTabParam } from '@/components/shared/use-tab-param'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { PageActions } from '@/components/shared/page-actions'
import {
  usePermissions,
  useObjectPermissions,
  useEffectivePermissions,
} from '@/lib/auth/use-permissions'
import { useConversationTagsEnabled } from '@/features/system-preferences/queries'

const formatDate = (value: string) =>
  new Date(value).toLocaleDateString(undefined, { dateStyle: 'long' })

// Every tab this page can address, whatever the caller's authority — see `useTabParam`.
const TAB_VOCABULARY = ['overview', 'metrics', 'exports']

export function EvaluationDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { has, hasAll } = usePermissions()
  // Assigning a model needs the catalog (models:read) to pick one; editing an
  // existing assignment's params needs only evaluations:update.
  const canAssignModel = hasAll(['evaluations:update', 'models:read'])
  const query = useEvaluation(id ?? '')
  const conversationTagsEnabled = useConversationTagsEnabled()
  const evaluation = query.data
  const groupId = evaluation?.evaluation_group_id ?? ''
  const groupQuery = useEvaluationGroup(groupId)
  const group = groupQuery.data
  // Metrics are gated server-side by the parent group's configured metrics-access
  // levels (owner / `manage` break-glass always get the full aggregate; a
  // `members_personal_metrics` member gets a personal slice). The server folds that into
  // the group detail's `user_permissions`, injecting `evaluation_groups:view_metrics`
  // exactly when the caller would be admitted (any scope) — gate the fetch on it so a
  // non-admitted viewer doesn't 403 (and toast) on the endpoint. The response's `scope`
  // then drives the personal-view badge.
  const canViewMetrics = useObjectPermissions(group?.user_permissions).has(
    'evaluation_groups:view_metrics',
  )
  const metrics = useEvaluationMetrics(id ?? '', { enabled: canViewMetrics })
  const scenarios = useEvaluationScenarios(id ?? '')
  const scenarioItems = scenarios.data?.items ?? []
  // Past the list's one page the sum would cover only part of the scenarios the tile beside it counts.
  const allScenariosListed = scenarios.data && scenarios.data.items.length >= scenarios.data.total
  const taskCount = useTaskCount(
    allScenariosListed ? scenarios.data?.items.map((s) => s.id) : undefined,
  )
  // A reason only for what is known to be missing: a scenario list still loading, or one that
  // failed to load, is not an evaluation without scenarios.
  const startBlocked =
    (evaluation?.models?.length ?? 0) === 0
      ? 'Assign a model first'
      : scenarios.data?.items.length === 0
        ? 'Add a scenario first'
        : undefined
  const approve = useApproveEvaluation(id ?? '')
  const reject = useRejectEvaluation(id ?? '')
  const unassign = useUnassignModel(id ?? '')
  const duplicate = useDuplicateEvaluation()
  const [rejectOpen, setRejectOpen] = useState(false)
  const [duplicateOpen, setDuplicateOpen] = useState(false)
  const [includeChildren, setIncludeChildren] = useState(false)
  const [assignOpen, setAssignOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [startOpen, setStartOpen] = useState(false)
  // Export gates on the parent group's in-group owner / admin authority — the same
  // rule the server enforces (`assert_export_authority` → `evaluation_groups:export`,
  // `:manage` break-glass). Authority arrives with the async group fetch, so the
  // Exports tab appears once `group` resolves.
  const groupPerms = useObjectPermissions(group?.user_permissions)
  const canExport = groupPerms.hasAny(EXPORT_AUTHORITY_PERMISSIONS)
  // Conversation actions accept the permission from either source, unlike export authority
  // above, which the server resolves per object only.
  const conversationPerms = useEffectivePermissions(group?.user_permissions)
  // No layout space is reserved while authority resolves: holding it would collapse and shift
  // the body for the common viewer, who gets one tab and therefore no bar at all. The URL is
  // validated against the full vocabulary rather than the visible subset, because authority
  // arrives with the async group fetch — see `useTabParam`.
  const tabItems: TabItem[] = [
    { value: 'overview', label: 'Overview' },
    ...(canViewMetrics ? [{ value: 'metrics', label: 'Metrics' }] : []),
    ...(canExport ? [{ value: 'exports', label: 'Exports' }] : []),
  ]
  const [tab, setTab] = useTabParam(TAB_VOCABULARY, 'overview')
  const activeTab = tabItems.some((item) => item.value === tab) ? tab : 'overview'
  // No tablist means nothing for `aria-labelledby` to point at.
  const panel = tabItems.length > 1 ? tabPanelProps : () => ({})
  const [unassignTarget, setUnassignTarget] = useState<{ id: string; name: string } | null>(null)
  const [editTarget, setEditTarget] = useState<{
    id: string
    name: string | null
    inherited: Record<string, unknown>
    paramsDisabled: boolean
  } | null>(null)

  // Honor where the user came from (groups list, "All evaluations" tab, group detail);
  // fall back to the parent group on a deep link (no in-app history).
  const cameFromApp = location.key !== 'default'
  const backTarget = groupId ? `/evaluation-groups/${groupId}` : '/evaluation-groups'

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => (cameFromApp ? navigate(-1) : navigate(backTarget))}
      >
        <ArrowLeft className="size-4" />{' '}
        {cameFromApp ? 'Back' : group ? `Back to ${group.title}` : 'Back'}
      </Button>

      {query.isPending && <DetailSkeleton />}
      {query.isError && <p className="text-destructive">{humanizeError(query.error)}</p>}

      {evaluation && (
        <>
          <GroupIdentity groupId={groupId} path={[{ label: evaluation.title }]} />

          <header className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="font-display text-2xl font-semibold tracking-tight">
                {evaluation.title}
              </h1>
              <EvaluationStatusBadge status={evaluation.status} />
            </div>

            <Card>
              <CardContent className="pt-6">
                <dl className="grid gap-4 sm:grid-cols-3">
                  <Field label="Evaluation group">
                    <Link
                      to={`/evaluation-groups/${evaluation.evaluation_group_id}`}
                      className="underline underline-offset-2"
                    >
                      {group ? (group.title ?? 'Untitled draft') : '…'}
                    </Link>
                  </Field>
                  <Field label="Date added">{formatDate(evaluation.created_at)}</Field>
                  {/* Evaluations carry no deadline of their own; the group's end date is it. */}
                  {group?.end_date && (
                    <Field label="Complete by">{formatDate(`${group.end_date}T00:00:00`)}</Field>
                  )}
                  {evaluation.description && (
                    <Field label="Evaluation description" className="sm:col-span-3">
                      <span className="whitespace-pre-line">{evaluation.description}</span>
                    </Field>
                  )}
                </dl>
              </CardContent>
            </Card>

            {evaluation.rejection_reason && (
              <div className="border-destructive/40 bg-destructive/10 rounded-md border px-4 py-3 text-sm">
                <span className="text-destructive font-medium">Rejected</span> —{' '}
                {evaluation.rejection_reason}
              </div>
            )}

            {/* What the evaluation asks for, for whoever runs it; a red teamer sees their own
                conversations below instead. Same authority as Exports. */}
            {canExport && (
              <div className="grid grid-cols-3 gap-3">
                <StatTile label="Models to test" value={evaluation.models?.length ?? 0} />
                <StatTile label="Scenarios to test" value={scenarios.data?.total ?? '—'} />
                <StatTile label="Tasks to complete" value={taskCount ?? '—'} />
              </div>
            )}

            <PageActions
              primary={
                <>
                  {conversationPerms.has('conversations:create') && (
                    <Button
                      size="sm"
                      disabled={startBlocked !== undefined || !scenarios.data}
                      title={startBlocked}
                      onClick={() => {
                        // One scenario leaves nothing to choose, so the dialog would be a dead click.
                        const only = scenarioItems.length === 1 ? scenarioItems[0] : undefined
                        if (only)
                          navigate(
                            `/evaluations/${evaluation.id}/conversation-groups/new?scenario=${only.id}`,
                          )
                        else setStartOpen(true)
                      }}
                    >
                      <MessageSquarePlus /> Start a conversation
                    </Button>
                  )}
                  {has('evaluations:approve') && evaluation.status === 'under_review' && (
                    <>
                      <Button
                        size="sm"
                        disabled={approve.isPending}
                        onClick={() => approve.mutate()}
                      >
                        Approve
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => setRejectOpen(true)}>
                        Reject
                      </Button>
                    </>
                  )}
                </>
              }
              secondary={[
                {
                  key: 'duplicate',
                  label: 'Duplicate',
                  icon: Copy,
                  onSelect: () => {
                    setIncludeChildren(false)
                    setDuplicateOpen(true)
                  },
                  disabled: duplicate.isPending,
                  // Duplicate makes a new evaluation, so it gates on the global create
                  // capability, not this evaluation's per-group authority (the backend still
                  // enforces write access on the source). The copy lands in this group, so it
                  // also needs the group to accept evaluations (approved/published, the backend
                  // 409s otherwise); like Exports, the gate resolves with the async group fetch.
                  when: has('evaluations:create') && groupAcceptsEvaluations(group?.status),
                },
                {
                  key: 'edit',
                  label: 'Edit',
                  icon: Pencil,
                  onSelect: () => navigate(`/evaluations/${evaluation.id}/edit`),
                  when: has('evaluations:update'),
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
          </header>

          <ConversationsSection
            evaluation={evaluation}
            groupPermissions={group?.user_permissions}
          />

          {tabItems.length > 1 && <Tabs tabs={tabItems} value={activeTab} onChange={setTab} />}

          {activeTab === 'overview' && (
            <div {...panel('overview')} className="space-y-8">
              <section aria-labelledby="evaluation-settings" className="space-y-3">
                <h2
                  id="evaluation-settings"
                  className="font-display text-lg font-semibold tracking-tight"
                >
                  Evaluation settings
                </h2>
                <div
                  className={cn(
                    'grid grid-cols-[minmax(0,1fr)] items-start gap-4',
                    evaluation.tags_enabled ? 'lg:grid-cols-3' : 'lg:grid-cols-2',
                  )}
                >
                  <Card>
                    <CardHeader>
                      <div className="flex items-center justify-between gap-2">
                        <h3 className="leading-none font-semibold tracking-tight">
                          Models ({evaluation.models?.length ?? 0})
                        </h3>
                        {canAssignModel && (
                          <Button size="sm" variant="outline" onClick={() => setAssignOpen(true)}>
                            <Plus className="size-4" /> Assign model
                          </Button>
                        )}
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      {(evaluation.models ?? []).map((model) => (
                        <div
                          key={model.assignment_id}
                          className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm"
                        >
                          <div className="min-w-0">
                            <div className="truncate">{model.name ?? '— masked —'}</div>
                            <div className="text-muted-foreground truncate text-xs">
                              {[model.provider, model.provider_model_id]
                                .filter(Boolean)
                                .join(' · ')}
                            </div>
                          </div>
                          {has('evaluations:update') && (
                            <div className="flex shrink-0 items-center">
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label="Edit model parameters"
                                onClick={() =>
                                  setEditTarget({
                                    id: model.assignment_id,
                                    name: model.name ?? null,
                                    inherited: model.effective_parameters ?? {},
                                    paramsDisabled: model.advanced_params_disabled,
                                  })
                                }
                              >
                                <SlidersHorizontal className="size-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label="Unassign model"
                                disabled={unassign.isPending}
                                onClick={() =>
                                  setUnassignTarget({
                                    id: model.assignment_id,
                                    name: model.name ?? 'this model',
                                  })
                                }
                              >
                                <Trash2 className="size-4" />
                              </Button>
                            </div>
                          )}
                        </div>
                      ))}
                      {(evaluation.models?.length ?? 0) === 0 && (
                        <p className="text-muted-foreground text-sm">
                          No models yet. Assign one to start conversations.
                        </p>
                      )}
                    </CardContent>
                  </Card>

                  {conversationTagsEnabled && evaluation.tags_enabled && (
                    <AllowedTagsCard
                      evaluationId={evaluation.id}
                      restricted={evaluation.tags_restricted}
                      canManage={has('evaluations:update')}
                    />
                  )}

                  <Card>
                    <CardHeader>
                      <h3 className="leading-none font-semibold tracking-tight">Other details</h3>
                    </CardHeader>
                    <CardContent>
                      <dl className="space-y-3">
                        <Field label="Data license">
                          <LicenseValue license={evaluation.effective_license} />
                          {!evaluation.data_license_id && groupQuery.isSuccess && (
                            <span className="text-muted-foreground ml-1.5 text-xs">
                              {group?.data_license_id
                                ? '(inherited from group)'
                                : '(platform default)'}
                            </span>
                          )}
                        </Field>
                        <Field label="Models masked">
                          {evaluation.mask_models_enabled ? 'Yes' : 'No'}
                        </Field>
                        <Field label="Updated">
                          {new Date(evaluation.updated_at).toLocaleString(undefined, {
                            dateStyle: 'long',
                            timeStyle: 'short',
                          })}
                        </Field>
                      </dl>
                    </CardContent>
                  </Card>
                </div>
              </section>

              <ScenariosSection evaluationId={evaluation.id} />
            </div>
          )}

          {activeTab === 'metrics' && metrics.data && (
            <div {...panel('metrics')}>
              <EvaluationMetricsPanel metrics={metrics.data} />
            </div>
          )}
          {activeTab === 'metrics' && !metrics.data && (
            <Card {...panel('metrics')}>
              <CardHeader>
                <CardTitle>Metrics</CardTitle>
              </CardHeader>
              <CardContent>
                {/* Error copy only when there is nothing to show: a failed background refetch
                    keeps `metrics.data`, so the panel above stays up instead. */}
                <p
                  className={
                    metrics.isError ? 'text-destructive text-sm' : 'text-muted-foreground text-sm'
                  }
                >
                  {metrics.isError ? humanizeError(metrics.error) : 'Loading metrics…'}
                </p>
              </CardContent>
            </Card>
          )}

          {activeTab === 'exports' && (
            <div {...panel('exports')} className="bg-card rounded-lg border p-4">
              <ExportsSection scope={{ evaluation_id: evaluation.id }} />
            </div>
          )}

          <ConfirmDialog
            open={duplicateOpen}
            onOpenChange={setDuplicateOpen}
            title="Duplicate evaluation"
            description={
              <div className="space-y-3">
                <p>
                  This creates a new evaluation you own in the same group, copied from this one.
                </p>
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5 size-4 rounded border"
                    checked={includeChildren}
                    onChange={(e) => setIncludeChildren(e.target.checked)}
                  />
                  <span>Also copy its model assignments, scenarios, and tasks.</span>
                </label>
              </div>
            }
            confirmLabel="Duplicate"
            pending={duplicate.isPending}
            onConfirm={() =>
              duplicate.mutate(
                { sourceId: evaluation.id, includeChildren },
                {
                  onSuccess: (created) => {
                    setDuplicateOpen(false)
                    // Duplicate gates on create, but the edit route needs update — fall back to detail.
                    navigate(
                      has('evaluations:update')
                        ? `/evaluations/${created.id}/edit`
                        : `/evaluations/${created.id}`,
                    )
                  },
                },
              )
            }
          />

          <ConfirmDialog
            open={rejectOpen}
            onOpenChange={setRejectOpen}
            title="Reject evaluation"
            description="Record why this evaluation is being rejected."
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

          {canAssignModel && (
            <AssignModelDialog
              evaluationId={evaluation.id}
              open={assignOpen}
              onOpenChange={setAssignOpen}
            />
          )}

          {has('evaluations:update') && (
            <AssignModelDialog
              key={editTarget?.id}
              evaluationId={evaluation.id}
              open={editTarget !== null}
              onOpenChange={(open) => !open && setEditTarget(null)}
              assignmentId={editTarget?.id}
              modelName={editTarget?.name}
              inherited={editTarget?.inherited}
              advancedParamsDisabled={editTarget?.paramsDisabled}
            />
          )}

          <StartConversationDialog
            evaluationId={evaluation.id}
            scenarios={scenarioItems}
            open={startOpen}
            onOpenChange={setStartOpen}
          />

          <ExportDialog
            open={exportOpen}
            onOpenChange={setExportOpen}
            scope={{ evaluation_id: evaluation.id }}
            title="Export evaluation"
          />

          <ConfirmDialog
            open={unassignTarget !== null}
            onOpenChange={(open) => !open && setUnassignTarget(null)}
            title="Remove model"
            // No browsable tombstone surface for assignments, so the Undo on the toast that
            // follows is the only way back — and it is what re-opens the conversations too.
            description={`Remove ${unassignTarget?.name} from this evaluation? Conversations already run with it are deleted too. You can undo this from the confirmation that follows.`}
            confirmLabel="Remove"
            destructive
            pending={unassign.isPending}
            onConfirm={() =>
              unassignTarget &&
              unassign.mutate(unassignTarget.id, { onSuccess: () => setUnassignTarget(null) })
            }
          />
        </>
      )}
    </div>
  )
}
