import { toast } from 'sonner'
import { Checkbox } from '@/components/ui/checkbox'
import { cn } from '@/lib/utils'
import type { RailMember, TaskCompletionUI } from './task-completion-ui'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

// Shared by the inline note and the blocked row's toast, which must agree.
const READ_ONLY_NOTE = 'Read-only — not your conversation.'
const COUNTS_UNAVAILABLE_NOTE = 'Completion counts unavailable — reload to retry.'
// Says why there are no numbers, rather than leaving the roll-up silently absent. Worded to
// hold for a caller who owns *some* of the group too, not only one who owns none.
const COUNTS_PRIVATE_NOTE = "Completion progress isn't shown for conversations you don't own."
// One checkbox stands for several conversations, so the rail says which.
const ALL_MODELS_NOTE = 'Checking a task marks it complete for every model in this conversation.'
const OWNED_MODELS_NOTE =
  'Checking a task marks it complete for the models you own; the others count toward the total only.'

// What to say below the list. The counts notes outrank the scope: they explain the missing
// roll-up, which is the more surprising absence. A failed read outranks even the privacy note:
// reads are only mounted when some member is readable, so `error` implies one of the caller's
// *own* reads failed — the reload it asks for is a reload they can actually act on.
function railNote(completion: TaskCompletionUI): string | null {
  if (completion.mode === 'toggle') return completion.canToggle ? null : READ_ONLY_NOTE
  const { members, countsState, countsReadable } = completion
  if (countsState === 'error') return COUNTS_UNAVAILABLE_NOTE
  if (!countsReadable) return COUNTS_PRIVATE_NOTE
  if (members.length === 0) return null
  if (members.every((m) => m.canToggle)) return ALL_MODELS_NOTE
  return members.some((m) => m.canToggle) ? OWNED_MODELS_NOTE : READ_ONLY_NOTE
}

// How many of `members` have this task checked off (the "K").
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

  const note = railNote(completion)

  return (
    <div className="bg-card space-y-2 rounded-lg border p-4">
      <div className="text-muted-foreground font-mono text-[10px]">Scenario</div>
      {scenario ? (
        <>
          <p className="font-medium">{scenario.name}</p>
          {scenario.description && (
            <p className="text-muted-foreground text-sm">{scenario.description}</p>
          )}
          {tasks.length > 0 && (
            <div className="space-y-2 border-t pt-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-muted-foreground font-mono text-[10px]">Tasks</div>
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

// A group's task row: one checkbox for the whole comparison, applied to every member the
// caller owns, plus the K/N badge counting all members. The checkbox reflects only the members
// it changes, so a mixed group's box can't claim a state it would not set.
function RollupTaskRow({
  task,
  completion,
}: {
  task: TaskResponse
  completion: Extract<TaskCompletionUI, { mode: 'rollup' }>
}) {
  const { members, countsState, countsReadable } = completion
  const count = completedCount(task.id, members)
  // Struck through only once every conversation has it and the counts are settled. The
  // readability conjunct is belt-and-braces — an unreadable member reads as empty, so `count`
  // already falls short — except against a cache entry left by a pre-ownership-change read.
  const done =
    countsReadable && countsState === 'ready' && members.length > 0 && count === members.length
  const rollup = {
    ready: `Completed for ${count} of ${members.length} models`,
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

  const owned = members.filter((m) => m.canToggle)
  if (owned.length === 0) {
    return (
      <li className={cn(tileClass(done), 'justify-between')}>
        <TaskLabel task={task} done={done} />
        {badge}
      </li>
    )
  }

  const ownedDone = completedCount(task.id, owned)
  const checked = ownedDone === 0 ? false : ownedDone === owned.length ? true : 'indeterminate'
  const countsFailed = countsState === 'error'
  return (
    <li className={cn(tileClass(done), !countsFailed && 'hover:border-border')}>
      <label
        className={cn(
          'flex min-w-0 flex-1 items-start gap-2',
          countsFailed ? 'cursor-not-allowed' : 'cursor-pointer',
        )}
      >
        <Checkbox
          className="mt-0.5"
          checked={checked}
          // Counts that aren't established would render the box unchecked, so no toggle is
          // offered either way — but the two reasons differ. `loading` clears on its own, so a
          // real `disabled` is honest. A failed read does not: `retry: 1` and no
          // refetch-on-focus mean the control would sit dead and out of the tab order with its
          // explanation elsewhere. That one stays reachable and says so.
          disabled={countsState === 'loading' || completion.togglingTaskIds.has(task.id)}
          aria-disabled={countsFailed || undefined}
          onCheckedChange={(next) => {
            if (countsFailed) {
              toast.info(COUNTS_UNAVAILABLE_NOTE, { id: 'task-counts-unavailable' })
              return
            }
            completion.onToggle(task.id, next === true)
          }}
        />
        <TaskLabel task={task} done={done} />
      </label>
      {badge}
    </li>
  )
}
