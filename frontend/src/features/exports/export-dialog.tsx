import { useState } from 'react'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { FilterSelect } from '@/components/shared/filter-select'
import { Input } from '@/components/ui/input'
import { FormField } from '@/components/shared/form-field'
import { humanizeError } from '@/lib/api/problem'
import { cn } from '@/lib/utils'
import { uuid } from '@/lib/uuid'
import { SearchSelect } from '@/components/shared/search-select'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useRoles } from '@/features/users/queries'
import { useEvaluationScenarios, useScenarioTasks } from '@/features/scenarios/queries'
import { useGroupMembers, useGroupAnnotators } from '@/features/evaluation-groups/queries'
import type { ExportFilters, ExportFormat } from '@/lib/api/types'
import { exportFilenameStem, useExportTemplates, type ExportScope } from './queries'
import { useCreateExportJob } from './mutations'
import { useExports } from './exports-context'

type ExportDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: ExportScope
  title?: string
}

// Which filter dimensions each template honors — mirrors the backend's apply-where-applicable
// (a filter the template can't use is simply not shown, never sent).
const STATUS_TEMPLATES = new Set(['flags', 'reviews'])
const SCENARIO_TEMPLATES = new Set(['flags', 'conversations', 'transcript', 'engagement_report'])
const TASK_TEMPLATES = new Set(['flags'])
// Templates where `user_id` means a red-teamer (author/owner). The reviews export maps `user_id`
// onto reviewer_id — a different actor with no clean picker source (external annotators aren't
// group members) — so the "Red-teamer" filter is deliberately NOT offered for reviews.
const USER_TEMPLATES = new Set([
  'flags',
  'conversations',
  'conversation-groups',
  'transcript',
  'engagement_report',
])
// The reviews export maps `user_id` onto reviewer_id; its candidates are the group's assignable
// annotators (a different source than red-teamers), so it gets its own picker.
const REVIEWER_TEMPLATES = new Set(['reviews'])
// FlagStatus and ReviewStatus share these values.
const STATUS_OPTIONS = ['pending', 'approved', 'rejected'] as const

// Pick a catalog template + format + optional filters and queue a background job; closes at once
// (the store tracks the job, the nav shows progress, a toast + the downloads list surface the file).
export function ExportDialog({ open, onOpenChange, scope, title = 'Export' }: ExportDialogProps) {
  const templates = useExportTemplates(open)
  const create = useCreateExportJob()
  const { trackExport } = useExports()

  const [selected, setSelected] = useState('')
  const [format, setFormat] = useState<ExportFormat>('csv')
  const [createdFrom, setCreatedFrom] = useState('')
  const [createdTo, setCreatedTo] = useState('')
  const [status, setStatus] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [taskId, setTaskId] = useState('')
  const [userId, setUserId] = useState('')
  const [userSearch, setUserSearch] = useState('')
  const [reviewerSearch, setReviewerSearch] = useState('')

  const reset = () => {
    setSelected('')
    setFormat('csv')
    setCreatedFrom('')
    setCreatedTo('')
    setStatus('')
    setScenarioId('')
    setTaskId('')
    setUserId('')
    setUserSearch('')
    setReviewerSearch('')
  }

  // Scenario/task lists need a single evaluation; the member list needs a group. Only one scope
  // discriminant is set, so the "off" hook is passed '' (disabled) and never fires.
  const evaluationId = 'evaluation_id' in scope ? scope.evaluation_id : ''
  const groupId = 'evaluation_group_id' in scope ? scope.evaluation_group_id : ''
  const scenarios = useEvaluationScenarios(evaluationId)
  const tasks = useScenarioTasks(scenarioId)

  const items = templates.data?.items ?? []
  // A control shows only when its dimension applies to the template AND its list can be populated
  // for this scope (scenarios/tasks need an evaluation; the user/reviewer lists need a group).
  const showStatus = STATUS_TEMPLATES.has(selected)
  const showScenario = SCENARIO_TEMPLATES.has(selected) && evaluationId !== ''
  const showTask = TASK_TEMPLATES.has(selected) && showScenario && scenarioId !== ''
  const perms = usePermissions()
  // The annotators endpoint (reviewer picker's source) is server-gated on manage-members, so match
  // it here — otherwise a reviews-export caller who can't manage the group hits a 403 + error toast.
  const canManageMembers = perms.hasAny([
    'evaluation_groups:manage',
    'evaluation_groups:manage_members',
  ])
  const showReviewer = groupId !== '' && REVIEWER_TEMPLATES.has(selected) && canManageMembers
  // Author (red-teamer) picker: server-side search over the group's red_teamer members. Resolving the
  // role id needs GET /roles (global roles:read), but export authority is object-scoped — so gate the
  // roles fetch on the permission and, lacking it, hide the picker rather than 403 + a dead empty box.
  const canReadRoles = perms.has('roles:read')
  const redTeamerRoleId = useRoles({ enabled: canReadRoles }).data?.items.find(
    (role) => role.name === 'red_teamer',
  )?.id
  // Group scope only; the author picker additionally needs the resolved role id (so its first fetch is
  // role-filtered, not every member) — which also hides it when the caller lacks roles:read.
  const showUser = groupId !== '' && USER_TEMPLATES.has(selected) && !!redTeamerRoleId
  const memberPicker = useGroupMembers(groupId, {
    search: userSearch,
    roleId: redTeamerRoleId,
    enabled: showUser,
    limit: 20,
    keepPrevious: true,
  })
  const reviewerPicker = useGroupAnnotators(groupId, {
    search: reviewerSearch,
    enabled: showReviewer,
  })
  // A typed-but-unselected term would silently export unfiltered while the box still reads the text.
  // Block the export and surface an inline hint until the free text is resolved to a picked option.
  const userTermUnresolved = showUser && userSearch.trim() !== '' && userId === ''
  const reviewerTermUnresolved = showReviewer && reviewerSearch.trim() !== '' && userId === ''
  const pickerUnresolved = userTermUnresolved || reviewerTermUnresolved
  // A native date input's YYYY-MM-DD value sorts lexicographically = chronologically, so a string
  // compare catches an inverted range before it queues an export that would silently come back empty.
  const rangeInvalid = createdFrom !== '' && createdTo !== '' && createdFrom > createdTo

  const onExport = () => {
    if (!selected || rangeInvalid || pickerUnresolved) return
    const filters: ExportFilters = {}
    // The native date input is a timezone-less calendar day; interpret it in the operator's LOCAL
    // day and convert to a UTC instant (the backend compares UTC timestamps). `to` is the local
    // end-of-day at millisecond precision (the finest a JS Date carries) so the last second isn't
    // silently dropped.
    if (createdFrom) filters.created_from = new Date(`${createdFrom}T00:00:00`).toISOString()
    if (createdTo) filters.created_to = new Date(`${createdTo}T23:59:59.999`).toISOString()
    if (showStatus && status) filters.status = status
    if (showScenario && scenarioId) filters.scenario_id = scenarioId
    if (showTask && taskId) filters.task_id = taskId
    // Both pickers write the shared `userId` (red-teamer for USER templates, reviewer for reviews).
    if ((showUser || showReviewer) && userId) filters.user_id = userId
    const hasFilters = Object.keys(filters).length > 0

    create.mutate(
      // One fresh key per click: it lets the server dedup a network-level retry of *this* request
      // (same key + body). It does NOT dedup a second click (that mints a new key) — the
      // disabled-while-pending Export button is what guards double-clicks.
      {
        template: selected,
        format,
        ...(hasFilters ? { filters } : {}),
        ...scope,
        idempotency_key: uuid(),
      },
      {
        onSuccess: (job) => {
          trackExport({ id: job.id, template: job.template }, exportFilenameStem(scope))
          onOpenChange(false)
        },
      },
    )
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      onOpen={reset}
      dismissable
      className="max-w-lg"
    >
      <div className="max-h-[60vh] overflow-y-auto pr-1">
        {templates.isPending ? (
          <p className="text-muted-foreground text-sm">Loading templates…</p>
        ) : templates.isError ? (
          <p className="text-destructive text-sm">{humanizeError(templates.error)}</p>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground text-sm">No export templates available.</p>
        ) : (
          <div className="space-y-4">
            <fieldset className="space-y-2" disabled={create.isPending}>
              <legend className="sr-only">Export template</legend>
              {items.map((template) => (
                <label
                  key={template.key}
                  className={cn(
                    'flex items-start gap-2 rounded-md border p-2 text-sm',
                    selected === template.key ? 'border-primary bg-primary/5' : 'hover:bg-muted/50',
                  )}
                >
                  <input
                    type="radio"
                    name="export-template"
                    className="mt-0.5 size-4"
                    value={template.key}
                    checked={selected === template.key}
                    onChange={() => {
                      // Reset per-template filters so a stale scenario/task/status/user can't leak across templates.
                      setSelected(template.key)
                      setScenarioId('')
                      setTaskId('')
                      setStatus('')
                      setUserId('')
                      setUserSearch('')
                      setReviewerSearch('')
                    }}
                  />
                  <span>
                    <span className="font-medium">{template.name}</span>
                    <span className="text-muted-foreground block text-xs">
                      {template.description}
                    </span>
                  </span>
                </label>
              ))}
            </fieldset>

            <fieldset className="space-y-3" disabled={create.isPending}>
              <legend className="text-sm font-medium">Format</legend>
              <div className="flex gap-4 text-sm">
                {(['csv', 'json'] as const).map((fmt) => (
                  <label key={fmt} className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="export-format"
                      className="size-4"
                      value={fmt}
                      checked={format === fmt}
                      onChange={() => setFormat(fmt)}
                    />
                    <span>{fmt}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            {selected && (
              <fieldset className="space-y-3" disabled={create.isPending}>
                <legend className="text-sm font-medium">Filters (optional)</legend>
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Created from" htmlFor="export-from">
                    <Input
                      id="export-from"
                      type="date"
                      value={createdFrom}
                      onChange={(e) => setCreatedFrom(e.target.value)}
                    />
                  </FormField>
                  <FormField label="Created to" htmlFor="export-to">
                    <Input
                      id="export-to"
                      type="date"
                      value={createdTo}
                      onChange={(e) => setCreatedTo(e.target.value)}
                    />
                  </FormField>
                </div>
                {rangeInvalid && (
                  <p className="text-destructive text-xs">
                    “Created from” must be on or before “Created to”.
                  </p>
                )}

                {showStatus && (
                  <FormField label="Status" htmlFor="export-status">
                    <FilterSelect
                      id="export-status"
                      label="Status"
                      value={status}
                      onChange={setStatus}
                      allLabel="— any —"
                      options={STATUS_OPTIONS.map((s) => ({ value: s, label: s }))}
                    />
                  </FormField>
                )}

                {showScenario && (
                  <FormField label="Scenario" htmlFor="export-scenario">
                    <FilterSelect
                      id="export-scenario"
                      label="Scenario"
                      value={scenarioId}
                      onChange={(v) => {
                        setScenarioId(v)
                        setTaskId('') // the task list is scoped to the chosen scenario
                      }}
                      allLabel="— any —"
                      options={(scenarios.data?.items ?? []).map((s) => ({
                        value: s.id,
                        label: s.name,
                      }))}
                    />
                  </FormField>
                )}

                {showTask && (
                  <FormField label="Task" htmlFor="export-task">
                    <FilterSelect
                      id="export-task"
                      label="Task"
                      value={taskId}
                      onChange={setTaskId}
                      allLabel="— any —"
                      options={(tasks.data?.items ?? []).map((t) => ({
                        value: t.id,
                        label: t.name,
                      }))}
                    />
                  </FormField>
                )}

                {showUser && (
                  <SearchSelect
                    key={selected}
                    label="Red-teamer"
                    htmlFor="export-user"
                    value={userId}
                    onChange={setUserId}
                    onSearchChange={setUserSearch}
                    isPending={memberPicker.isLoading}
                    isError={memberPicker.isError}
                    error={
                      userTermUnresolved
                        ? 'Pick a red-teamer from the list, or clear the box.'
                        : undefined
                    }
                    options={(memberPicker.data?.items ?? []).map((m) => ({
                      value: m.user.id,
                      label: m.user.email,
                    }))}
                  />
                )}

                {showReviewer && (
                  <SearchSelect
                    key={selected}
                    label="Reviewer"
                    htmlFor="export-reviewer"
                    value={userId}
                    onChange={setUserId}
                    onSearchChange={setReviewerSearch}
                    isPending={reviewerPicker.isLoading}
                    isError={reviewerPicker.isError}
                    error={
                      reviewerTermUnresolved
                        ? 'Pick a reviewer from the list, or clear the box.'
                        : undefined
                    }
                    options={(reviewerPicker.data?.items ?? []).map((a) => ({
                      value: a.id,
                      label: a.email,
                    }))}
                  />
                )}
              </fieldset>
            )}
          </div>
        )}
      </div>
      <div className="flex justify-end gap-2 border-t pt-4">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
          Cancel
        </Button>
        <Button
          disabled={!selected || rangeInvalid || create.isPending || pickerUnresolved}
          onClick={onExport}
        >
          {create.isPending ? 'Starting…' : 'Export'}
        </Button>
      </div>
    </Modal>
  )
}
