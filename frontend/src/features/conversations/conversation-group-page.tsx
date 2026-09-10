import { useCallback, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Pencil, Plus, Trash2 } from 'lucide-react'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { GroupIdentity } from '@/features/evaluation-groups/group-trail'
import { useGroupTrail } from '@/features/evaluation-groups/use-group-trail'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { FormField } from '@/components/shared/form-field'
import { useAuth } from '@/lib/auth/auth-context'
import { useGroupAuthority } from './use-group-authority'
import { AuthorityError } from './authority-error'
import { useEvaluation, useEvaluationTagKeys } from '@/features/evaluations/queries'
import { useScenario, useScenarioTasks } from '@/features/scenarios/queries'
import { ScenarioRail } from '@/features/scenarios/scenario-rail'
import { togglingKey } from '@/features/scenarios/task-completion-ui'
import { useConversationGroup } from './queries'
import { useAddConversationToGroup, useDeleteConversationGroup } from './mutations'
import { useMembersCompletedTasks, useToggleTaskCompletion } from './task-completions'
import { memberA11yLabel, memberLabel, memberNameSuffixes } from './member-names'
import { RenameGroupDialog } from './conversations-section'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { ConversationPane } from './conversation-pane'
import type { AllowedKeysStatus } from './tag-key-field'
import { Button } from '@/components/ui/button'
import { PageActions } from '@/components/shared/page-actions'
import { Input } from '@/components/ui/input'
import { Modal } from '@/components/ui/modal'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, fieldErrorsFromProblem } from '@/lib/api/problem'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import type {
  ConversationGroupResponse,
  ConversationResponse,
  EvaluationResponse,
} from '@/lib/api/types'

export function ConversationGroupPage() {
  const { id, groupId } = useParams<{ id: string; groupId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const evalId = id ?? ''
  const gid = groupId ?? ''

  const authority = useGroupAuthority(evalId, 'conversations:read')
  const { allows, canManage, groupPermissions } = authority
  const evaluation = useEvaluation(evalId)
  const groupQuery = useConversationGroup(evalId, gid, { groupPermissions })
  const del = useDeleteConversationGroup(evalId)

  const group = groupQuery.data
  const members = group?.conversations ?? []
  // `read_any` surfaces other members' groups read-only, so writing needs the owner check too —
  // except for the `evaluation_groups:manage` break-glass, which the write services honour.
  const canWrite = Boolean(group && user && group.user_id === user.id) || canManage
  // Resolved by id, not looked up in a scenario list: the list is one page deep, so a
  // scenario past it would be indistinguishable from a tombstone. The by-id read is
  // live-only, so `null` is the tombstone — and its tasks endpoint would 404, don't fetch.
  const scenarioQuery = useScenario(evalId, group?.scenario_id)
  const scenario = scenarioQuery.data ?? undefined
  const scenarioMissing = scenarioQuery.data === null
  const tasks = useScenarioTasks(scenario?.id ?? '')
  // Completions are readable only where the caller authored them: the list route is
  // author-scoped unless `evaluation_groups:manage`, and authoring is owner-only, so a member
  // the caller doesn't own answers `[]` whatever its owner ticked. Not `canToggleConversation`
  // below — that also demands `conversations:update`, which reading does not.
  const canReadCompletions = (c: ConversationResponse) => canManage || c.user_id === user?.id
  // The roll-up needs *every* member readable — a partial count is exactly the kind of number
  // the rail must not assert.
  const countsReadable = members.every(canReadCompletions)
  // Per member, not the group roll-up endpoint: the rail checks tasks off per conversation,
  // and these are the same cache entries the conversation page writes — so a toggle there
  // is already reflected here, and the K/N roll-up derives from one source.
  //
  // Gated on *any* readable member, not all: in a mixed group the caller's own row must still
  // show true state and toggle, and `canToggle` implies ownership implies readable — so this
  // guarantees every live checkbox has a mounted query behind it. A supervisor who owns none
  // reads nothing, which is the case worth saving the requests on. Ids stay unfiltered because
  // the returned sets are index-aligned with them.
  const memberCompletions = useMembersCompletedTasks(
    members.some(canReadCompletions) ? members.map((m) => m.id) : [],
  )
  const toggleCompletion = useToggleTaskCompletion()
  // Keyed per (member, task), so a toggle disables only the row it belongs to.
  const [togglingKeys, setTogglingKeys] = useState<Set<string>>(new Set())
  // Owner-only, break-glass excluded — same rule as `canFlag` below. Check-off resolves
  // the conversation under the caller's ownership and `evaluation_groups:manage` does not
  // lift that, so a toggle a non-owner reached would 404.
  const canToggleConversation = (c: ConversationResponse) =>
    c.user_id === user?.id && allows('conversations:update')
  const onToggleTask = (conversationId: string, taskId: string, next: boolean) => {
    // The rail renders no checkbox for a member this returns false for, so this is a
    // safety net — kept because its sibling on the conversation page has one.
    const member = members.find((c) => c.id === conversationId)
    if (!member || !canToggleConversation(member)) return
    const key = togglingKey(conversationId, taskId)
    setTogglingKeys((prev) => new Set(prev).add(key))
    toggleCompletion.mutate(
      { conversationId, taskId, next },
      {
        onSettled: () =>
          setTogglingKeys((prev) => {
            const nextSet = new Set(prev)
            nextSet.delete(key)
            return nextSet
          }),
      },
    )
  }

  const [input, setInput] = useState('')
  const [prompt, setPrompt] = useState('')
  const [promptNonce, setPromptNonce] = useState(0)
  const [renameOpen, setRenameOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  // Conversations whose (warmup-enabled) model is still warming. Each pane reports
  // its readiness up so the broadcast can refuse to send while any model is cold.
  const [warmingConvs, setWarmingConvs] = useState<Set<string>>(new Set())
  const reportWarmup = useCallback((conversationId: string, blocked: boolean) => {
    setWarmingConvs((prev) => {
      if (blocked === prev.has(conversationId)) return prev // no change — keep the ref stable
      const next = new Set(prev)
      if (blocked) next.add(conversationId)
      else next.delete(conversationId)
      return next
    })
  }, [])
  const anyWarming = warmingConvs.size > 0

  const modelName = (assignmentId: string) =>
    (evaluation.data?.models ?? []).find((m) => m.assignment_id === assignmentId)?.name ??
    '— masked —'

  // Named once for both consumers below — the panes and the rail's per-member rows must
  // carry the same name for the same conversation, or the disambiguating number is worse
  // than none.
  const memberNames = members.map((c) => ({
    title: c.title,
    modelName: modelName(c.evaluation_ai_model_id),
  }))
  const nameSuffixes = memberNameSuffixes(memberNames)

  const warmupEnabled = (assignmentId: string) =>
    (evaluation.data?.models ?? []).find((m) => m.assignment_id === assignmentId)?.warmup_enabled ??
    false

  const tagsEnabled = evaluation.data?.tags_enabled === true
  // Only a restricted evaluation has a key whitelist to advertise in the composers.
  const tagsRestricted = tagsEnabled && evaluation.data?.tags_restricted === true
  const tagKeys = useEvaluationTagKeys(tagsRestricted ? evalId : '')
  const allowedTagKeys = tagsRestricted ? (tagKeys.data ?? []).map((row) => row.key) : null
  // An unsettled query must not read as "restricted with nothing allowed": that labels every existing
  // key stale and invites deleting a valid tag, and on an error the false state never clears.
  // Unrestricted evaluations have no query to wait for, so they are settled by definition.
  const allowedKeysStatus: AllowedKeysStatus = tagsRestricted ? tagKeys.status : 'success'
  const acceptsImages = (assignmentId: string) =>
    (evaluation.data?.models ?? [])
      .find((m) => m.assignment_id === assignmentId)
      ?.input_modalities?.includes('image') ?? false

  const broadcast = () => {
    const content = input.trim()
    if (!content || anyWarming) return
    setPrompt(content)
    setPromptNonce((n) => n + 1)
    setInput('')
  }

  const evalTitle = evaluation.data?.title
  const { groupTitle } = useGroupTrail(evaluation.data?.evaluation_group_id ?? '')

  if (authority.pending) return <DetailSkeleton />
  if (authority.failed)
    return <AuthorityError error={authority.error} backTo={`/evaluations/${evalId}`} />
  if (!authority.granted) return <NotAuthorized />

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2 self-start"
        onClick={() => navigate(`/evaluations/${evalId}`)}
      >
        <ArrowLeft className="size-4" /> {evalTitle ? `Back to ${evalTitle}` : 'Back to evaluation'}
      </Button>

      {groupQuery.isPending && <p className="text-muted-foreground">Loading…</p>}
      {groupQuery.isError && (
        <p className="text-destructive">Error: {(groupQuery.error as Error).message}</p>
      )}

      {group && evaluation.data && (
        <>
          <GroupIdentity groupId={evaluation.data.evaluation_group_id} />
          <Breadcrumbs
            items={[
              ...(groupTitle
                ? [
                    {
                      label: groupTitle,
                      to: `/evaluation-groups/${evaluation.data.evaluation_group_id}`,
                    },
                  ]
                : []),
              { label: evalTitle ?? 'Evaluation', to: `/evaluations/${evalId}` },
              { label: group.name },
            ]}
          />
          <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="space-y-1">
              <h1 className="font-display text-2xl font-semibold tracking-tight">{group.name}</h1>
              <p className="text-muted-foreground font-mono text-xs">
                {members.length} model{members.length === 1 ? '' : 's'} side by side
              </p>
            </div>
            <PageActions
              secondary={[
                {
                  key: 'add-model',
                  label: 'Add model',
                  icon: Plus,
                  onSelect: () => setAddOpen(true),
                  // The create posts to the group's scenario, so a tombstoned one is a
                  // route-level 404: dead-end, don't offer it.
                  disabled: scenarioMissing,
                  when: allows('conversations:create') && canWrite,
                },
                {
                  key: 'rename',
                  label: 'Rename',
                  icon: Pencil,
                  onSelect: () => setRenameOpen(true),
                  when: allows('conversations:update') && canWrite,
                },
                {
                  key: 'delete',
                  label: 'Delete conversation',
                  icon: Trash2,
                  onSelect: () => setDeleteOpen(true),
                  destructive: true,
                  when: allows('conversations:delete') && canWrite,
                },
              ]}
            />
          </header>

          <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
            <div className="space-y-4">
              {/* Min-width panes in a horizontal scroller: 2 fit side by side, 3+ scroll
                  without crushing each transcript. */}
              <div className="flex gap-4 overflow-x-auto pb-2">
                {members.map((c, i) => (
                  <ConversationPane
                    key={c.id}
                    evaluationId={evalId}
                    conversationId={c.id}
                    assignmentId={c.evaluation_ai_model_id}
                    modelName={modelName(c.evaluation_ai_model_id)}
                    nameSuffix={nameSuffixes[i]}
                    warmupEnabled={warmupEnabled(c.evaluation_ai_model_id)}
                    acceptsImages={acceptsImages(c.evaluation_ai_model_id)}
                    tagsEnabled={tagsEnabled}
                    allowedTagKeys={allowedTagKeys}
                    allowedKeysStatus={allowedKeysStatus}
                    tags={c.tags}
                    onWarmupChange={reportWarmup}
                    title={c.title}
                    prompt={prompt}
                    promptNonce={promptNonce}
                    groupPermissions={groupPermissions}
                    canWrite={canWrite}
                    // Per member, and without the `canManage` arm `canWrite` carries: the flag
                    // services filter on the conversation's own `user_id`, so a break-glass
                    // viewer offered the control would get a 404.
                    canFlag={c.user_id === user?.id && allows('flags:create')}
                  />
                ))}
              </div>

              {/* Broadcasting writes to every member conversation, which `read_any` never widens —
                  so a group surfaced to its owner read-only carries no send form. */}
              {canWrite && (
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    broadcast()
                  }}
                  className="bg-background sticky bottom-0 flex gap-2 py-2"
                >
                  <Textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        broadcast()
                      }
                    }}
                    aria-label="Message all models"
                    placeholder={
                      anyWarming
                        ? 'Waiting for every model to warm up…'
                        : 'Message all models… (Enter to send, Shift+Enter for newline)'
                    }
                    rows={2}
                    className="min-h-0"
                  />
                  <Button type="submit" disabled={!input.trim() || anyWarming}>
                    Send to all
                  </Button>
                </form>
              )}
            </div>

            <aside className="min-w-0 space-y-4 lg:sticky lg:top-4">
              {scenarioMissing ? (
                <div className="bg-card rounded-lg border p-4">
                  <p className="text-muted-foreground text-sm">Scenario no longer available.</p>
                </div>
              ) : (
                <ScenarioRail
                  scenario={scenario}
                  tasks={tasks.data?.items ?? []}
                  completion={{
                    mode: 'rollup',
                    members: members.map((c, i) => ({
                      conversationId: c.id,
                      label: memberLabel(memberNames[i]!, nameSuffixes[i]),
                      a11yLabel: memberA11yLabel(memberNames[i]!, nameSuffixes[i]),
                      completedTaskIds:
                        memberCompletions.completedTaskIdsByConversation[i] ?? new Set(),
                      canToggle: canToggleConversation(c),
                    })),
                    onToggle: onToggleTask,
                    togglingKeys,
                    countsState: memberCompletions.state,
                    countsReadable,
                  }}
                />
              )}
            </aside>
          </div>

          <RenameGroupDialog
            evaluationId={evalId}
            group={group}
            open={renameOpen}
            onOpenChange={setRenameOpen}
          />
          <AddModelDialog
            evaluation={evaluation.data}
            group={group}
            open={addOpen}
            onOpenChange={setAddOpen}
          />
          <ConfirmDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            title="Delete conversation"
            description={`Delete this conversation and everything in it? ${REVERSIBLE_DELETE_NOTE}`}
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() =>
              del.mutate(
                { groupId: gid, conversationIds: members.map((m) => m.id) },
                { onSuccess: () => navigate(`/evaluations/${evalId}`) },
              )
            }
          />
        </>
      )}
    </div>
  )
}

function AddModelDialog({
  evaluation,
  group,
  open,
  onOpenChange,
}: {
  evaluation: EvaluationResponse
  group: ConversationGroupResponse
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const add = useAddConversationToGroup(group.scenario_id, group.id)
  const models = evaluation.models ?? []
  const [modelId, setModelId] = useState(models[0]?.assignment_id ?? '')
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (!modelId) return
    setError(null)
    try {
      await add.mutateAsync({
        evaluation_ai_model_id: modelId,
        conversation_group_id: group.id,
        title: title.trim() || undefined,
      })
      onOpenChange(false)
    } catch (err) {
      // Only field errors (422) are suppressed by the global handler — surface the
      // title reason inline; everything else (403/404/5xx/network) is toasted globally.
      setError(err instanceof ApiError ? (fieldErrorsFromProblem(err.problem).title ?? null) : null)
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Add model"
      onOpen={() => {
        setModelId(models[0]?.assignment_id ?? '')
        setTitle('')
        setError(null)
      }}
    >
      <div className="space-y-4">
        {error && <p className="text-destructive text-sm">{error}</p>}
        <FormField label="Model" htmlFor="add-model">
          <Select value={modelId} onValueChange={setModelId}>
            <SelectTrigger id="add-model">
              <SelectValue placeholder="— select model —" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {models.map((m) => (
                  <SelectItem key={m.assignment_id} value={m.assignment_id}>
                    {m.name ?? '— masked —'}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="Title (optional)" htmlFor="add-model-title">
          <Input
            id="add-model-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Roleplay framing"
            maxLength={255}
          />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" disabled={!modelId || add.isPending} onClick={submit}>
            {add.isPending ? 'Adding…' : 'Add'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
