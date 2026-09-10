import { useEffect, useId, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Ban as BanIcon, Flag, Pencil, Tag, Trash2 } from 'lucide-react'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { GroupIdentity } from '@/features/evaluation-groups/group-trail'
import { useGroupTrail } from '@/features/evaluation-groups/use-group-trail'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { useGroupAuthority } from './use-group-authority'
import { AuthorityError } from './authority-error'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import { EditTagsDialog } from './edit-tags-dialog'
import type { AllowedKeysStatus } from './tag-key-field'
import type { MessageResponse } from '@/lib/api/types'
import { unsentTagKeys } from './tag-rows'
import { useConversation, useConversationGroup, useMessages } from './queries'
import { useDeleteConversation, useRenameConversation } from './mutations'
import { useCompletedTasks, useToggleTaskCompletion } from './task-completions'
import { useConversationStream } from './use-conversation-stream'
import { Bubble } from './bubble'
import { LabelsTruncatedNote } from '@/features/annotations/labels-truncated-note'
import { AnnotateMessageDialog } from '@/features/annotations/annotate-message-dialog'
import { indexAnnotations } from '@/features/annotations/annotation-index'
import { useConversationAnnotations } from '@/features/annotations/queries'
import { ChatComposer } from './chat-composer'
import { AssistantActions } from './assistant-actions'
import { useWarmupGate } from './use-model-warmup'
import { WarmupBadge } from './warmup-badge'
import { FlagMessageDialog } from '@/features/message-flags/flag-message-dialog'
import { useMessageFlags } from '@/features/message-flags/queries'
import { useEvaluation, useEvaluationTagKeys } from '@/features/evaluations/queries'
import { useScenario, useScenarioTasks } from '@/features/scenarios/queries'
import { ScenarioRail } from '@/features/scenarios/scenario-rail'
import { Button } from '@/components/ui/button'
import { PageActions } from '@/components/shared/page-actions'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Modal } from '@/components/ui/modal'
import { FormField } from '@/components/shared/form-field'
import { cn } from '@/lib/utils'
import { ApiError, fieldErrorsFromProblem, humanizeError } from '@/lib/api/problem'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { StatusPill } from '@/components/shared/status-pill'

export function ConversationDetailPage() {
  const { id, conversationId } = useParams<{ id: string; conversationId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const globalPerms = usePermissions()
  const evalId = id ?? ''
  const convId = conversationId ?? ''
  const del = useDeleteConversation(evalId)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [renameOpen, setRenameOpen] = useState(false)
  const [tagsOpen, setTagsOpen] = useState(false)
  const authority = useGroupAuthority(evalId, 'conversations:read')
  const { allows, canManage, groupPermissions } = authority
  const evaluation = useEvaluation(evalId)
  const conversation = useConversation(evalId, convId, { groupPermissions })
  const messages = useMessages(evalId, convId, { groupPermissions })
  const stream = useConversationStream(evalId, convId)
  const [flagIds, setFlagIds] = useState<string[] | null>(null)
  const [annotateId, setAnnotateId] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)
  const toggleSelect = (mid: string) =>
    setSelected((s) => (s.includes(mid) ? s.filter((x) => x !== mid) : [...s, mid]))

  const conv = conversation.data
  const group = useConversationGroup(evalId, conv?.conversation_group_id ?? '', {
    groupPermissions,
  })
  const annotationsQ = useConversationAnnotations(convId)
  const sorted = [...(messages.data?.items ?? [])].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  )
  // A turn's reply record is proof of what was actually sent — reconciled into `messageUnsentTags`
  // below, in place of the policy's guess, for any key it carries.
  const recordedByTurn = new Map<string, Record<string, string>>()
  for (const m of sorted) {
    if (m.role === 'assistant' && m.tag_context && Object.keys(m.tag_context).length > 0) {
      recordedByTurn.set(m.turn_id, m.tag_context)
    }
  }
  const lastAssistant = [...sorted].reverse().find((m) => m.role === 'assistant')
  const annotationIndex = indexAnnotations(
    annotationsQ.data?.items ?? [],
    sorted.map((m) => m.id),
  )
  const assignment = conv
    ? (evaluation.data?.models ?? []).find((m) => m.assignment_id === conv.evaluation_ai_model_id)
    : undefined
  const modelName = assignment?.name ?? undefined
  // Tagging is a per-evaluation opt-out: with it off the backend rejects every tagged write, so no
  // tag surface renders. Read as off until the evaluation resolves — revealing a control late beats
  // offering one that would 400.
  const tagsEnabled = evaluation.data?.tags_enabled === true
  // Only a restricted evaluation has a key whitelist to advertise; unrestricted stays free-form,
  // so the query is skipped there rather than fetching a list the authoring UI wouldn't use.
  const tagsRestricted = tagsEnabled && evaluation.data?.tags_restricted === true
  const tagKeys = useEvaluationTagKeys(tagsRestricted ? evalId : '')
  const allowedTagKeys = tagsRestricted ? (tagKeys.data ?? []).map((row) => row.key) : null
  // An unsettled query must not read as "restricted with nothing allowed": that labels every existing
  // key stale and invites deleting a valid tag, and on an error the false state never clears.
  // Unrestricted evaluations have no query to wait for, so they are settled by definition.
  const allowedKeysStatus: AllowedKeysStatus = tagsRestricted ? tagKeys.status : 'success'
  // The stored tags the backend will not fold into the prompt — same rules as the server, so the
  // chips below don't present filtered-out context as something the model received. With tagging
  // off it folds nothing, so every stored tag is unsent: marking only the valueless ones would
  // leave the rest looking like context the model received.
  // Only claim anything once the evaluation itself has resolved. `tagsEnabled` reads as `false` while
  // that query is pending or failed — the safe direction for *offering* a control, and the wrong one for
  // a *claim about history*: the conversation query can resolve first, and marking every chip "not sent"
  // on an evaluation whose flags never arrived asserts the opposite of what happened.
  // Conversation-level chips answer a different question than the per-message ones: they describe
  // what the *next* turn will send under today's policy, while a bubble describes what its own turn
  // did send. The same key can therefore read "not sent" here and unmarked below.
  const unsentTags = evaluation.isSuccess
    ? unsentTagKeys(conv?.tags ?? {}, {
        tagsEnabled,
        allowedKeys: allowedTagKeys,
        keysSettled: allowedKeysStatus === 'success',
      })
    : new Set<string>()
  // Per message, on the same rules: a message tag outside the policy is dropped from the fold too.
  // A turn's record (`recordedByTurn`) replaces that guess entirely — see below.
  const messageUnsentTags = (message: MessageResponse) => {
    if (!evaluation.isSuccess) return new Set<string>()
    const unsent = unsentTagKeys(message.tags ?? {}, {
      tagsEnabled,
      allowedKeys: allowedTagKeys,
      keysSettled: allowedKeysStatus === 'success',
    })
    const record = recordedByTurn.get(message.turn_id)
    // A non-empty record decides both ways: what it carries was sent, what it omits was not. The
    // policy guess only survives where the turn has no record at all. Raw keys compare equal to the
    // backend's sanitised comparison (`TagFoldPolicy.unsent_for_message`), because a tag key is
    // charset-validated (`TAG_KEY_PATTERN`) to characters sanitising never touches.
    if (record) return new Set(Object.keys(message.tags ?? {}).filter((key) => !(key in record)))
    return unsent
  }
  const unsentHintId = `${useId()}-unsent-tags`
  const acceptsImages = assignment?.input_modalities?.includes('image') ?? false
  // Resolved by id, not looked up in a scenario list: the list is one page deep, so a
  // scenario past it would be indistinguishable from a tombstone. The by-id read is
  // live-only, so `null` is the tombstone — and its tasks endpoint would 404, don't fetch.
  const scenarioQuery = useScenario(evalId, conv?.scenario_id)
  const scenario = scenarioQuery.data ?? undefined
  const scenarioMissing = scenarioQuery.data === null
  const tasks = useScenarioTasks(scenario?.id ?? '')
  const taskList = tasks.data?.items ?? []
  const completions = useCompletedTasks(convId)
  const completedTaskIds = new Set(completions.data ?? [])
  const toggleCompletion = useToggleTaskCompletion()
  // Task ids with a toggle in flight, so the rail disables only those rows (not the
  // whole checklist) and focus stays on the row the user just clicked.
  const [togglingTaskIds, setTogglingTaskIds] = useState<Set<string>>(new Set())
  // Check-off authoring is owner-only on the backend (like flags): a break-glass viewer
  // (evaluation_groups:manage) can open someone else's conversation but a toggle would 404.
  // The rail marks a non-owner's rows aria-disabled and explains the block itself (toast +
  // an inline reason), returning before `onToggle` — so this guard is only a safety net.
  const isOwner = Boolean(conv && user && conv.user_id === user.id)
  // Writes stay owner-scoped, with one exception the services honour: the
  // `evaluation_groups:manage` break-glass lifts the owner predicate on PATCH/DELETE and on the
  // message routes, so hiding these from an admin would take away something that works.
  const canWrite = isOwner || canManage
  // Flag authoring is owner-only *even* for the break-glass: `resolve_conversation_context`
  // filters on `user_id` with no `can_manage` arm, so on a transcript `read_any` surfaced the
  // whole flow would 404 — for a group owner (no `flags:create` at all) and an admin alike.
  const canFlag = isOwner && allows('flags:create')
  // Deliberately NOT `allows` (global ∪ in-group role) like the gates above: the annotations
  // routes use the plain global gate, which reads the JWT claim — and that claim carries global
  // roles only. An in-group annotator holding no global key would be offered the affordance and
  // refused with a 403. There is also no ownership predicate to satisfy, unlike flags.
  const canAnnotate = globalPerms.has('annotations:create')
  const canToggleTasks = allows('conversations:update') && isOwner
  const onToggleTask = (taskId: string, next: boolean) => {
    if (!canToggleTasks) return
    setTogglingTaskIds((s) => new Set(s).add(taskId))
    toggleCompletion.mutate(
      { conversationId: convId, taskId, next },
      {
        onSettled: () =>
          setTogglingTaskIds((s) => {
            const nextSet = new Set(s)
            nextSet.delete(taskId)
            return nextSet
          }),
      },
    )
  }
  const flags = useMessageFlags(
    { conversation_id: convId, limit: 50, offset: 0 },
    { groupPermissions },
  )
  const flagList = flags.data?.items ?? []
  // Warming a model is a write (`conversations:update` on the warmup route), so it is gated like
  // one: the in-group `owner` role that `read_any` surfaces this transcript to holds no
  // `conversations:update`, and probing anyway reports the 403 as the model being unavailable.
  // `canWrite` is ownership-shaped where the route is permission-shaped — restrictive-but-correct
  // only while messaging itself stays owner-scoped. Lift that, and this needs the permission too.
  const warmup = useWarmupGate(
    evalId,
    conv?.evaluation_ai_model_id ?? '',
    Boolean(assignment?.warmup_enabled) && canWrite,
  )
  // The composer renders under the conversation query, but the warmup gate derives
  // from the evaluation query (which carries the assignment + its warmup flag). Until
  // that resolves we don't yet know whether the model needs warming, so hold sending —
  // otherwise a deep-link could fire the first message into a still-cold endpoint.
  const composerBlocked = warmup.blocked || evaluation.isPending

  // Keep the latest message in view as the transcript grows / streams.
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: 'end' })
  }, [sorted.length, stream.streaming?.assistantText, stream.streaming?.userText])

  const evalTitle = evaluation.data?.title
  const { groupTitle } = useGroupTrail(evaluation.data?.evaluation_group_id ?? '')
  const hasRail = Boolean(conv) || flagList.length > 0

  const selectMode = selected.length > 0

  const chatColumn = (
    <div className="space-y-3">
      {selectMode && canFlag && (
        <div className="border-primary/40 bg-primary/5 flex items-center justify-between rounded-md border px-3 py-2 text-sm">
          <span className="font-medium">{selected.length} selected</span>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setSelected([])}>
              Clear
            </Button>
            <Button size="sm" onClick={() => setFlagIds(selected)}>
              <Flag className="size-4" /> Flag {selected.length} for review
            </Button>
          </div>
        </div>
      )}

      {/* The conversation is its own surface, set apart from the page chrome — solid card, matching the side-by-side panes. */}
      <div className="bg-card flex flex-col overflow-hidden rounded-xl border">
        {/* Inside the card, above the scroll box: the mount effect scrolls the newest message into
            view, which takes the viewport with it, so anything up in the page chrome is off-screen
            on arrival — and this only renders on a transcript long enough for that to happen. */}
        <LabelsTruncatedNote total={annotationsQ.data?.total} className="border-b px-4 py-2" />
        <div className="max-h-[calc(100svh-18rem)] min-h-40 space-y-3 overflow-y-auto p-4">
          {sorted.map((m) => {
            const assistant = m.role === 'assistant'
            const isSelected = selected.includes(m.id)
            return (
              <div
                key={m.id}
                className={cn(
                  'group flex items-start gap-2 rounded-lg transition-colors',
                  isSelected && 'bg-primary/5 ring-primary/30 ring-1',
                )}
              >
                {assistant && canFlag && (
                  <input
                    type="checkbox"
                    className={cn(
                      'mt-3 ml-1 size-4 shrink-0 transition-opacity',
                      selectMode || isSelected
                        ? 'opacity-100'
                        : 'opacity-0 group-hover:opacity-100',
                    )}
                    checked={isSelected}
                    onChange={() => toggleSelect(m.id)}
                    aria-label="Select message to flag"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <Bubble
                    messageId={m.id}
                    role={m.role}
                    content={m.content}
                    status={m.status}
                    flagCount={m.flag_count}
                    extra={m.extra}
                    tagContext={m.tag_context}
                    tagContextPartial={m.tag_context_partial}
                    imageKeys={m.image_keys}
                    tags={m.tags}
                    unsentTags={messageUnsentTags(m)}
                    onFlag={assistant && canFlag ? () => setFlagIds([m.id]) : undefined}
                    annotations={annotationIndex.byMessage.get(m.id) ?? []}
                    onAnnotate={canAnnotate ? () => setAnnotateId(m.id) : undefined}
                  />
                </div>
              </div>
            )
          })}

          {stream.streaming?.userText !== undefined && (
            <Bubble
              role="user"
              content={stream.streaming.userText}
              imageKeys={stream.streaming.imageKeys}
            />
          )}
          {stream.streaming && (
            <Bubble role="assistant" content={stream.streaming.assistantText} status="streaming" />
          )}

          {messages.data && sorted.length === 0 && !stream.streaming && (
            <p className="text-muted-foreground py-8 text-center text-sm">
              No messages yet — send the first one below.
            </p>
          )}
          <div ref={endRef} />
        </div>

        {/* `read_any` widens reads only, so a member's transcript read by their group owner shows
            no composer rather than one whose every send fails as a lost connection. */}
        {canWrite && (
          <div className="bg-card/50 border-t p-3">
            {lastAssistant && !stream.streaming && (
              <AssistantActions
                onRegenerate={() => stream.regenerate(lastAssistant.id)}
                onContinue={() => stream.continueReply(lastAssistant.id)}
                disabled={stream.pending || composerBlocked}
              />
            )}
            <ChatComposer
              send={stream.send}
              pending={stream.pending}
              disabled={composerBlocked}
              acceptsImages={acceptsImages}
              tagsEnabled={tagsEnabled}
              allowedTagKeys={allowedTagKeys}
              allowedKeysStatus={allowedKeysStatus}
              placeholder={
                warmup.blocked
                  ? 'Waiting for the model to warm up…'
                  : evaluation.isPending
                    ? 'Loading…'
                    : 'Message the model… (Enter to send, Shift+Enter for newline)'
              }
              autoFocusKey={conv?.id}
            />
          </div>
        )}
      </div>
    </div>
  )

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

      {(conversation.isPending || messages.isPending) && (
        <p className="text-muted-foreground">Loading…</p>
      )}
      {conversation.isError && (
        <p className="text-destructive">Error: {humanizeError(conversation.error)}</p>
      )}
      {annotationsQ.isError && (
        <p className="text-destructive text-sm">
          Labels are unavailable: {humanizeError(annotationsQ.error)}
        </p>
      )}
      {conv && (
        <>
          {evaluation.data && <GroupIdentity groupId={evaluation.data.evaluation_group_id} />}
          <Breadcrumbs
            items={[
              ...(groupTitle && evaluation.data
                ? [
                    {
                      label: groupTitle,
                      to: `/evaluation-groups/${evaluation.data.evaluation_group_id}`,
                    },
                  ]
                : []),
              { label: evalTitle ?? 'Evaluation', to: `/evaluations/${evalId}` },
              ...(group.data
                ? [
                    {
                      label: group.data.name,
                      to: `/evaluations/${evalId}/conversation-groups/${conv.conversation_group_id}`,
                    },
                  ]
                : []),
              { label: conv.title ?? 'Single conversation' },
            ]}
          />
          <header className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <h1 className="font-display text-2xl font-semibold tracking-tight">
                {conv.title ?? 'Single conversation'}
              </h1>
              <p className="text-muted-foreground font-mono text-xs">
                Model: {modelName ?? '— masked —'}
              </p>
              {assignment?.warmup_enabled && canWrite && (
                <WarmupBadge state={warmup.state} onRetry={warmup.warmUp} />
              )}
            </div>
            <PageActions
              secondary={[
                {
                  key: 'rename',
                  label: 'Rename',
                  icon: Pencil,
                  onSelect: () => setRenameOpen(true),
                  when: allows('conversations:update') && canWrite,
                },
                {
                  key: 'tags',
                  label: Object.keys(conv?.tags ?? {}).length > 0 ? 'Edit tags' : 'Add tags',
                  icon: Tag,
                  onSelect: () => setTagsOpen(true),
                  when: allows('conversations:update') && canWrite && tagsEnabled,
                },
                {
                  key: 'delete',
                  label: 'Delete',
                  icon: Trash2,
                  onSelect: () => setDeleteOpen(true),
                  destructive: true,
                  when: allows('conversations:delete') && canWrite,
                },
              ]}
            />
          </header>

          {/* Shown whatever the flag says: `tags_enabled` gates authoring and the prompt fold, and the
              backend keeps the stored map either way. Hiding it would drop the record of what context
              the recorded turns were sent with, which is the one thing a reviewer comes here for. */}
          {Object.keys(conv.tags ?? {}).length > 0 && (
            <div className="space-y-1">
              {/* `role="list"` spelled out: the reset drops list-style, and Safari then drops the list
                  semantics with it. Without the label these chips read as loose text before the transcript. */}
              <ul
                role="list"
                aria-label="Tags"
                aria-describedby={unsentTags.size > 0 ? unsentHintId : undefined}
                className="flex flex-wrap gap-1.5"
                data-testid="conversation-tags"
              >
                {/* Capped and titled because a value runs to 512 chars. */}
                {Object.entries(conv.tags ?? {}).map(([k, v]) => (
                  <li key={k}>
                    <Badge variant="tag" className="max-w-[18rem]" title={`${k}: ${v}`}>
                      <span className="truncate">
                        {k}: {v}
                      </span>
                      {/* The state rides an icon plus text for a reader, never opacity: at `opacity-60`
                          this chip measured 2.35:1 against its own background in the light theme
                          (4.70:1 undimmed) at 12px, so the only marker of "not sent" would be the
                          thing that makes it unreadable — and hover/`title` reaches neither keyboard
                          nor touch. */}
                      {unsentTags.has(k) && (
                        <>
                          <BanIcon className="ml-1 size-3 shrink-0" aria-hidden />
                          <span className="sr-only">(not sent to the model)</span>
                        </>
                      )}
                    </Badge>
                  </li>
                ))}
              </ul>
              {evaluation.isSuccess && !tagsEnabled ? (
                // Gated on the same answer as the marking: `tagsEnabled` is `false` while the query is
                // unsettled, so without this the page would say "tagging is off" about an evaluation it
                // has not read yet. Carries the id too — with tagging off every key is unsent, so this is
                // the paragraph the list's `aria-describedby` resolves to.
                <p id={unsentHintId} className="text-muted-foreground text-xs">
                  Tagging is off for this evaluation — these are kept but not sent to the model.
                </p>
              ) : (
                // Named as well as marked per chip: the list is jumpable (a reader can land on it
                // without the surrounding prose), and the dialog labels the same key "(no longer
                // allowed)" one click away.
                unsentTags.size > 0 && (
                  <p id={unsentHintId} className="text-muted-foreground text-xs">
                    {/* Sorted, so the same set reads in the same order here and in the export
                        columns, which render `TagFoldPolicy.unsent`. */}
                    Kept but not sent to the model: {[...unsentTags].sort().join(', ')} —{' '}
                    {allowedTagKeys === null
                      ? 'a tag with no value.'
                      : 'a key this evaluation no longer allows, or a tag with no value.'}
                  </p>
                )
              )}
            </div>
          )}

          <div
            className={cn(
              'grid items-start gap-6',
              hasRail && 'lg:grid-cols-[minmax(0,1fr)_320px]',
            )}
          >
            {chatColumn}
            {hasRail && (
              <aside className="min-w-0 space-y-4 lg:sticky lg:top-4">
                {scenarioMissing ? (
                  <div className="bg-card rounded-lg border p-4">
                    <p className="text-muted-foreground text-sm">Scenario no longer available.</p>
                  </div>
                ) : (
                  conv &&
                  !completions.isPending && (
                    <ScenarioRail
                      scenario={scenario}
                      tasks={taskList}
                      completion={{
                        mode: 'toggle',
                        completedTaskIds,
                        onToggle: onToggleTask,
                        canToggle: canToggleTasks,
                        togglingTaskIds,
                      }}
                    />
                  )
                )}

                {flagList.length > 0 && (
                  <div className="bg-card space-y-2 rounded-lg border p-4">
                    <div className="text-muted-foreground font-mono text-[10px] tracking-[0.2em] uppercase">
                      Flags ({flagList.length})
                    </div>
                    <div className="space-y-1.5">
                      {flagList.map((f) => (
                        <Link
                          key={f.id}
                          to={`/message-flags/${f.id}`}
                          className="hover:bg-muted/50 block rounded-md border px-2.5 py-1.5"
                        >
                          <div className="flex items-start gap-1.5">
                            <Flag
                              className={cn(
                                'mt-0.5 size-3 shrink-0',
                                f.red_flagged ? 'text-warn' : 'text-muted-foreground',
                              )}
                            />
                            <span className="line-clamp-2 text-sm">{f.reason}</span>
                          </div>
                          <div className="text-muted-foreground mt-1 flex items-center gap-2 text-xs">
                            <span>
                              {f.messages.length} msg{f.messages.length === 1 ? '' : 's'}
                            </span>
                            <StatusPill status={f.status} />
                          </div>
                        </Link>
                      ))}
                    </div>
                  </div>
                )}
              </aside>
            )}
          </div>
        </>
      )}

      <FlagMessageDialog
        conversationId={convId}
        messageIds={flagIds ?? []}
        open={flagIds !== null}
        onOpenChange={(o) => {
          if (!o) setFlagIds(null)
        }}
        onSuccess={() => setSelected([])}
      />

      <AnnotateMessageDialog
        messageId={annotateId ?? ''}
        conversationId={convId}
        annotations={annotationIndex.byMessage.get(annotateId ?? '') ?? []}
        currentUserId={user?.id}
        open={annotateId !== null}
        onOpenChange={(o) => {
          if (!o) setAnnotateId(null)
        }}
      />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete single conversation"
        description={`Delete this single conversation? ${REVERSIBLE_DELETE_NOTE}`}
        confirmLabel="Delete"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          del.mutate(convId, { onSuccess: () => navigate(`/evaluations/${evalId}`) })
        }
      />

      <RenameConversationDialog
        evaluationId={evalId}
        conversationId={convId}
        currentTitle={conv?.title}
        open={renameOpen}
        onOpenChange={setRenameOpen}
      />

      <EditTagsDialog
        evaluationId={evalId}
        conversationId={convId}
        currentTags={conv?.tags ?? {}}
        allowedKeys={allowedTagKeys}
        keysStatus={allowedKeysStatus}
        open={tagsOpen}
        onOpenChange={setTagsOpen}
      />
    </div>
  )
}

function RenameConversationDialog({
  evaluationId,
  conversationId,
  currentTitle,
  open,
  onOpenChange,
}: {
  evaluationId: string
  conversationId: string
  currentTitle: string | null | undefined
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const rename = useRenameConversation(evaluationId)
  const [title, setTitle] = useState(currentTitle ?? '')
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setError(null)
    try {
      await rename.mutateAsync({ conversationId, title: title.trim() || null })
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
      title="Rename single conversation"
      onOpen={() => {
        setTitle(currentTitle ?? '')
        setError(null)
      }}
    >
      <div className="space-y-4">
        {error && <p className="text-destructive text-sm">{error}</p>}
        <FormField label="Title" htmlFor="rename-conversation-title">
          <Input
            id="rename-conversation-title"
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Direct ask"
            maxLength={255}
          />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={rename.isPending || title.trim() === (currentTitle ?? '')}
            onClick={submit}
          >
            {rename.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
