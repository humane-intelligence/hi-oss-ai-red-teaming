import { useState } from 'react'
import { Check, ChevronDown, ChevronRight, Minus } from 'lucide-react'
import { toast } from 'sonner'
import { Checkbox } from '@/components/ui/checkbox'
import { cn } from '@/lib/utils'
import { togglingKey } from './task-completion-ui'
import type { RailMember, TaskCompletionUI } from './task-completion-ui'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

// Shared by the toggle-mode inline note and the blocked row's toast, which must agree.
const READ_ONLY_NOTE = 'Read-only — not your conversation.'
const COUNTS_UNAVAILABLE_NOTE = 'Completion counts unavailable — reload to retry.'
// Says why there are no numbers, rather than leaving the roll-up silently absent. Worded to
// hold for a caller who owns *some* of the group too, not only one who owns none.
const COUNTS_PRIVATE_NOTE = "Completion progress isn't shown for conversations you don't own."
// Two strings, because "read-only" is a lie when some rows are live.
const NO_MEMBER_WRITABLE_NOTE = 'Read-only — not your conversations.'
const SOME_MEMBERS_WRITABLE_NOTE = "Conversations you don't own show state only."

// What to say below the list when a row can't be acted on. A mixed group is not read-only,
// so it doesn't get the read-only wording. The counts notes outrank ownership: they explain
// the missing roll-up, which is the more surprising absence. A failed read outranks even the
// privacy note: reads are only mounted when some member is readable, so `error` implies one of
// the caller's *own* reads failed — the reload it asks for is a reload they can actually act on.
function blockedRowNote(completion: TaskCompletionUI): string | null {
  if (completion.mode === 'toggle') return completion.canToggle ? null : READ_ONLY_NOTE
  const { members, countsState, countsReadable } = completion
  if (countsState === 'error') return COUNTS_UNAVAILABLE_NOTE
  if (!countsReadable) return COUNTS_PRIVATE_NOTE
  if (members.length === 0 || members.every((m) => m.canToggle)) return null
  return members.some((m) => m.canToggle) ? SOME_MEMBERS_WRITABLE_NOTE : NO_MEMBER_WRITABLE_NOTE
}

// How many of the group's conversations have this task checked off (the "K").
function completedCount(taskId: string, members: RailMember[]): number {
  return members.filter((m) => m.completedTaskIds.has(taskId)).length
}

export function ScenarioRail({
  scenario,
  tasks,
  completion,
}: {
  scenario: ScenarioResponse | undefined
  tasks: TaskResponse[]
  completion: TaskCompletionUI
}) {
  const total = tasks.length
  // Counts the caller may not read get no bar and no percentage at all — an indeterminate bar
  // still claims a number is coming, and there is none to come.
  const showProgress = completion.mode === 'toggle' || completion.countsReadable
  // Bar semantics differ by mode. Toggle (one conversation): tasks checked / total tasks.
  // Rollup (a group): the average fill across all (task × conversation) cells, so partial
  // progress shows — not only tasks completed in *every* conversation.
  let pct: number | null
  let progressLabel: string
  if (completion.mode === 'rollup') {
    const cells = total * completion.members.length
    const filled = tasks.reduce((sum, t) => sum + completedCount(t.id, completion.members), 0)
    pct = cells > 0 ? Math.round((filled / cells) * 100) : 0
    // Don't assert a percentage the reads haven't established — a member still loading is
    // indistinguishable from one with nothing completed.
    progressLabel = { ready: `${pct}%`, loading: '…', error: '—' }[completion.countsState]
    // Indeterminate: ARIA wants `aria-valuenow` omitted rather than a number, and 0 would
    // read as "0% done" when the truth is unknown.
    if (completion.countsState !== 'ready') pct = null
  } else {
    const doneCount = tasks.filter((t) => completion.completedTaskIds.has(t.id)).length
    pct = total > 0 ? Math.round((doneCount / total) * 100) : 0
    progressLabel = `${doneCount}/${total} done`
  }

  const note = blockedRowNote(completion)

  return (
    <div className="bg-card space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground font-mono text-[10px] tracking-[0.2em] uppercase">
        Scenario
      </div>
      {scenario ? (
        <>
          <p className="font-medium">{scenario.name}</p>
          {scenario.description && (
            <p className="text-muted-foreground text-sm">{scenario.description}</p>
          )}
          {tasks.length > 0 && (
            <div className="space-y-2 border-t pt-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-muted-foreground font-mono text-[10px] tracking-[0.2em] uppercase">
                  Tasks
                </div>
                {showProgress && (
                  <span className="text-muted-foreground font-mono text-[10px] tabular-nums">
                    {progressLabel}
                  </span>
                )}
              </div>
              {showProgress && (
                <div
                  className="bg-muted h-1.5 w-full overflow-hidden rounded-full"
                  role="progressbar"
                  aria-label="Tasks completed"
                  aria-valuenow={pct ?? undefined}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-busy={completion.mode === 'rollup' && completion.countsState === 'loading'}
                >
                  <div
                    className="bg-ok h-full rounded-full transition-[width] duration-500 ease-out motion-reduce:transition-none"
                    style={{ width: `${pct ?? 0}%` }}
                  />
                </div>
              )}
              <ul className="space-y-1.5">
                {tasks.map((t) => (
                  <TaskRow key={t.id} task={t} completion={completion} />
                ))}
              </ul>
              {note && <p className="text-muted-foreground text-xs">{note}</p>}
            </div>
          )}
        </>
      ) : (
        <p className="text-muted-foreground text-sm">Loading scenario…</p>
      )}
    </div>
  )
}

// Shared tile look: rounded bordered card that fades to the "ok" tint when done.
const tileClass = (done: boolean) =>
  cn(
    'flex items-start gap-2 rounded-md border p-2 text-sm transition-all duration-300 motion-reduce:transition-none',
    done ? 'border-ok/40 bg-ok/10' : 'border-transparent',
  )

function TaskLabel({ task, done }: { task: TaskResponse; done: boolean }) {
  return (
    <span
      className={cn(
        'min-w-0 flex-1 transition-colors duration-300',
        done && 'text-muted-foreground',
      )}
    >
      <span className={cn('font-medium', done && 'line-through')}>{task.name}</span>
      {task.description && (
        <span className="text-muted-foreground block text-xs">{task.description}</span>
      )}
    </span>
  )
}

function TaskRow({ task, completion }: { task: TaskResponse; completion: TaskCompletionUI }) {
  if (completion.mode === 'toggle') {
    const done = completion.completedTaskIds.has(task.id)
    const blocked = !completion.canToggle
    return (
      <li>
        <label
          className={cn(
            tileClass(done),
            blocked ? 'cursor-not-allowed' : 'hover:border-border cursor-pointer',
          )}
        >
          <Checkbox
            className="mt-0.5"
            checked={done}
            disabled={completion.togglingTaskIds.has(task.id)}
            // Focusable and aria-disabled rather than `disabled`, so keyboard and
            // screen-reader users can reach the row and hear why it does nothing.
            aria-disabled={blocked || undefined}
            onCheckedChange={(next) => {
              if (blocked) {
                // One toast id, so repeated attempts replace rather than stack.
                toast.info(READ_ONLY_NOTE, { id: 'task-read-only' })
                return
              }
              completion.onToggle(task.id, next === true)
            }}
          />
          <TaskLabel task={task} done={done} />
        </label>
      </li>
    )
  }
  return <RollupTaskRow task={task} completion={completion} />
}

// A group's task row: the K/N roll-up stays the collapsed summary, and expanding reveals
// one row per member — a checkbox where the caller owns the conversation, a static marker
// otherwise. Collapsed by default so the rail is as compact as a read-only roll-up, and
// per-conversation rather than a group-wide toggle because completion is per conversation
// on the backend.
function RollupTaskRow({
  task,
  completion,
}: {
  task: TaskResponse
  completion: Extract<TaskCompletionUI, { mode: 'rollup' }>
}) {
  const [open, setOpen] = useState(false)
  const { members, countsState, countsReadable } = completion
  const panelId = `task-completions-${task.id}`
  const count = completedCount(task.id, members)
  // Struck through only once every conversation has it and the counts are settled. The
  // readability conjunct is belt-and-braces — an unreadable member reads as empty, so `count`
  // already falls short — except against a cache entry left by a pre-ownership-change read.
  const done =
    countsReadable && countsState === 'ready' && members.length > 0 && count === members.length
  const rollup = {
    ready: `Completed in ${count} of ${members.length} conversations`,
    loading: 'Loading completion counts',
    error: 'Completion counts unavailable',
  }[countsState]
  const rollupText = { ready: `${count}/${members.length}`, loading: '…', error: '—' }[countsState]

  // Absent when the caller may not read the counts — the inline note carries the reason once
  // for the list instead of every row repeating it.
  const badge = countsReadable ? (
    <span
      className="text-muted-foreground mt-0.5 shrink-0 rounded-full border px-1.5 py-0.5 font-mono text-[10px] tabular-nums"
      // `aria-label` is ignored on a generic element, so the badge needs a role to carry
      // it — otherwise a screen reader announces the bare "1/2".
      role="img"
      title={rollup}
      aria-label={rollup}
    >
      {rollupText}
    </span>
  ) : null

  // Nothing to expand into when the caller can't write to any of the group's
  // conversations: the per-member breakdown is only there to be acted on, so a read-only
  // viewer gets the roll-up on its own — exactly what this view showed before.
  if (!members.some((m) => m.canToggle)) {
    return (
      <li className={cn(tileClass(done), 'justify-between')}>
        <TaskLabel task={task} done={done} />
        {badge}
      </li>
    )
  }

  return (
    <li className={cn(tileClass(done), 'flex-col p-0')}>
      <button
        type="button"
        className="hover:bg-muted/40 flex w-full items-start gap-2 rounded-md p-2 text-left"
        aria-expanded={open}
        // Only while the panel exists — a dangling IDREF is an axe failure.
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((prev) => !prev)}
      >
        {open ? (
          <ChevronDown className="mt-0.5 size-4 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="mt-0.5 size-4 shrink-0" aria-hidden />
        )}
        <TaskLabel task={task} done={done} />
        {badge}
      </button>
      {open && (
        <ul id={panelId} className="w-full space-y-0.5 py-2 pr-2 pl-8">
          {members.map((member) => (
            <MemberRow
              key={member.conversationId}
              member={member}
              task={task}
              completion={completion}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

function MemberRow({
  member,
  task,
  completion,
}: {
  member: RailMember
  task: TaskResponse
  completion: Extract<TaskCompletionUI, { mode: 'rollup' }>
}) {
  const checked = member.completedTaskIds.has(task.id)
  const countsLoading = completion.countsState === 'loading'
  const countsFailed = completion.countsState === 'error'
  const busy = completion.togglingKeys.has(togglingKey(member.conversationId, task.id))
  // Named for the conversation *and* the task, so the row stands alone in a screen
  // reader's element list — the task is only in the disclosure button above it.
  const label = `${task.name} — ${member.a11yLabel}`
  const name = (
    <span className="min-w-0 truncate font-mono" title={member.a11yLabel}>
      {member.label}
    </span>
  )

  // No checkbox at all on a conversation the caller can't write to: the state is still
  // worth showing, but an affordance that can only ever refuse isn't.
  if (!member.canToggle) {
    // Same rule as the checkbox below: an unresolved read is indistinguishable from
    // nothing completed, so the marker reports "unknown" rather than asserting either. A read
    // the caller may not make is unresolved in the same way — "not completed" here would be
    // the very claim the hidden roll-up exists to avoid, one surface deeper. Marking an
    // unreadable group's rows unknown is over-conservative for an owner who merely lacks
    // `conversations:update`; that errs quiet rather than wrong, and a per-member readable
    // flag isn't worth the plumbing.
    const countsKnown = completion.countsReadable && completion.countsState === 'ready'
    return (
      <li className="flex items-center gap-2 px-1.5 py-1 text-xs">
        <span
          className="text-muted-foreground grid size-4 shrink-0 place-content-center"
          role="img"
          aria-label={
            countsKnown
              ? `${label} — ${checked ? 'completed' : 'not completed'}`
              : `${label} — completion unknown`
          }
        >
          {countsKnown && checked ? <Check className="size-3.5" /> : <Minus className="size-3" />}
        </span>
        {name}
      </li>
    )
  }

  return (
    <li>
      <label
        className={cn(
          'flex items-center gap-2 rounded-md px-1.5 py-1 text-xs',
          countsFailed ? 'cursor-not-allowed' : 'hover:bg-muted cursor-pointer',
        )}
      >
        <Checkbox
          checked={checked}
          // Counts that aren't established would render every box unchecked, so no toggle
          // is offered either way — but the two reasons differ. `loading` clears on its
          // own, so a real `disabled` is honest. A failed read does not: `retry: 1` and no
          // refetch-on-focus mean the control would sit dead and out of the tab order with its
          // explanation elsewhere — the inline note, or the badge where one is shown. That one
          // stays reachable and says so.
          // Not exclusive: `countsState` is group-wide, so a sibling read failing while
          // this row is mid-toggle sets both. Harmless — `disabled` wins and both mean
          // not actionable.
          disabled={countsLoading || busy}
          aria-disabled={countsFailed || undefined}
          aria-label={label}
          onCheckedChange={(next) => {
            if (countsFailed) {
              toast.info(COUNTS_UNAVAILABLE_NOTE, { id: 'task-counts-unavailable' })
              return
            }
            completion.onToggle(member.conversationId, task.id, next === true)
          }}
        />
        {name}
      </label>
    </li>
  )
}
