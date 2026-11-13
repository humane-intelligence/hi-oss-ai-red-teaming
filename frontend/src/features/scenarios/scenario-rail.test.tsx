import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { ScenarioRail } from './scenario-rail'
import type { RailMember, RollupCountsState } from './task-completion-ui'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { info: vi.fn() } }))

const SCEN_ID = 'scen-0001-0000-0000-000000000000'
const TASK_ID = 'task-0001-0000-0000-000000000000'

const scenario: ScenarioResponse = {
  id: SCEN_ID,
  evaluation_id: 'eval-0001-0000-0000-000000000000',
  name: 'Prompt injection',
  description: 'Try to override system prompt',
  position: 1,
  required_reviews: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const task: TaskResponse = {
  id: TASK_ID,
  scenario_id: SCEN_ID,
  name: 'Basic injection',
  description: 'Attempt via user turn',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function renderToggleRail(canToggle: boolean, onToggle = vi.fn()) {
  render(
    <ScenarioRail
      scenario={scenario}
      tasks={[task]}
      completion={{
        mode: 'toggle',
        completedTaskIds: new Set(),
        onToggle,
        canToggle,
        togglingTaskIds: new Set(),
      }}
    />,
  )
  return { onToggle }
}

describe('ScenarioRail toggle mode', () => {
  it('toggles the task for an owner', async () => {
    const { onToggle } = renderToggleRail(true)

    await userEvent.click(screen.getByRole('checkbox'))

    expect(onToggle).toHaveBeenCalledWith(TASK_ID, true)
  })

  it('leaves a non-owner’s row focusable and explains the block instead of toggling', async () => {
    const { onToggle } = renderToggleRail(false)
    const box = screen.getByRole('checkbox')

    // aria-disabled, not `disabled`: a real disabled attribute drops the row out of the
    // tab order, which is what left keyboard and screen-reader users with no explanation.
    expect(box).toHaveAttribute('aria-disabled', 'true')
    expect(box).not.toBeDisabled()
    box.focus()
    expect(box).toHaveFocus()

    await userEvent.click(box)

    expect(onToggle).not.toHaveBeenCalled()
    expect(box).not.toBeChecked()
    expect(toast.info).toHaveBeenCalledWith('Read-only — not your conversation.', {
      id: 'task-read-only',
    })
  })

  it('explains the block on a keyboard activation too', async () => {
    const { onToggle } = renderToggleRail(false)
    const box = screen.getByRole('checkbox')

    box.focus()
    await userEvent.keyboard(' ')

    expect(onToggle).not.toHaveBeenCalled()
    expect(box).not.toBeChecked()
    expect(toast.info).toHaveBeenCalled()
  })
})

function member({
  conversationId,
  ...overrides
}: Partial<RailMember> & { conversationId: string }): RailMember {
  return { conversationId, completedTaskIds: new Set(), canToggle: true, ...overrides }
}

function renderRollupRail(
  members: RailMember[],
  opts?: {
    countsState?: RollupCountsState
    countsReadable?: boolean
    togglingTaskIds?: Set<string>
  },
) {
  const onToggle = vi.fn()
  render(
    <ScenarioRail
      scenario={scenario}
      tasks={[task]}
      completion={{
        mode: 'rollup',
        members,
        onToggle,
        togglingTaskIds: opts?.togglingTaskIds ?? new Set(),
        countsState: opts?.countsState ?? 'ready',
        countsReadable: opts?.countsReadable ?? true,
      }}
    />,
  )
  return { onToggle }
}

const ALL_MODELS_NOTE = 'Checking a task marks it complete for every model in this conversation.'
const OWNED_MODELS_NOTE =
  'Checking a task marks it complete for the models you own; the others count toward the total only.'
const done = () => new Set([TASK_ID])
const twoMembersDoneIn = (doneIn: readonly string[]) =>
  ['c1', 'c2'].map((id) =>
    member({ conversationId: id, completedTaskIds: new Set(doneIn.includes(id) ? [TASK_ID] : []) }),
  )

describe('ScenarioRail rollup mode', () => {
  // One of two done: the state older per-model check-offs can leave behind.
  const PARTIAL = [
    member({ conversationId: 'c1', completedTaskIds: done() }),
    member({ conversationId: 'c2' }),
  ]
  // A group a break-glass manager composed: the caller owns c1, someone else owns c2.
  const MIXED = [
    member({ conversationId: 'c1' }),
    member({ conversationId: 'c2', canToggle: false }),
  ]

  it('shows one checkbox per task beside the K/N roll-up, with no per-model rows', () => {
    renderRollupRail(PARTIAL)

    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Completed for 1 of 2 models')).toHaveTextContent('1/2')
    expect(screen.getByText(ALL_MODELS_NOTE)).toBeInTheDocument()
  })

  it.each([
    ['unchecked when no model has it', [], 'unchecked'],
    ['checked when every model has it', ['c1', 'c2'], 'checked'],
    ['mixed when only some models have it', ['c1'], 'mixed'],
  ] as const)('reads %s', (_name, doneIn, expected) => {
    renderRollupRail(twoMembersDoneIn(doneIn))

    const box = screen.getByRole('checkbox')
    if (expected === 'mixed') expect(box).toBePartiallyChecked()
    else if (expected === 'checked') expect(box).toBeChecked()
    else expect(box).not.toBeChecked()
  })

  it.each([
    ['completes the task from unchecked', [], true],
    ['completes the task from mixed', ['c1'], true],
    ['clears the task from checked', ['c1', 'c2'], false],
  ] as const)('%s', async (_name, doneIn, next) => {
    const { onToggle } = renderRollupRail(twoMembersDoneIn(doneIn))

    await userEvent.click(screen.getByRole('checkbox'))

    expect(onToggle).toHaveBeenCalledWith(TASK_ID, next)
  })

  it('counts every model in the roll-up, striking the task only when all have it', () => {
    renderRollupRail([
      member({ conversationId: 'c1', completedTaskIds: done() }),
      member({ conversationId: 'c2', completedTaskIds: done() }),
    ])

    expect(screen.getByLabelText('Completed for 2 of 2 models')).toHaveTextContent('2/2')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
  })

  it('reflects only the models the caller owns in a mixed group, and says so', () => {
    renderRollupRail([
      member({ conversationId: 'c1', completedTaskIds: done() }),
      member({ conversationId: 'c2', canToggle: false }),
    ])

    // The box sets c1 only, so c2's missing check-off must not make it read as partial.
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(screen.getByLabelText('Completed for 1 of 2 models')).toBeInTheDocument()
    expect(screen.getByText(OWNED_MODELS_NOTE)).toBeInTheDocument()
  })

  it('shows the task rows while a member’s completions load, without asserting a count', () => {
    renderRollupRail(PARTIAL, { countsState: 'loading' })

    // The block must NOT unmount: adding a model mounts a new query.
    expect(screen.getByText(task.name)).toBeInTheDocument()
    expect(screen.getByLabelText('Loading completion counts')).toHaveTextContent('…')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
    // Transient and self-clearing, so a real disabled is honest.
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('says the counts are unavailable when a member’s read failed', () => {
    renderRollupRail(PARTIAL, { countsState: 'error' })

    // Silently reading a failed member as "nothing completed" would under-report K.
    expect(screen.getByLabelText('Completion counts unavailable')).toHaveTextContent('—')
    // Inline too, not only on activation — a sighted mouse user must not have to click a
    // seemingly-live checkbox to find out why nothing happens.
    expect(screen.getByText('Completion counts unavailable — reload to retry.')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
  })

  it('leaves the box reachable and explains itself when the counts failed', async () => {
    const { onToggle } = renderRollupRail(PARTIAL, { countsState: 'error' })
    const box = screen.getByRole('checkbox')

    // A failed read doesn't recover on its own (retry: 1, no refetch on focus), so a real
    // `disabled` would leave a permanently dead control out of the tab order.
    expect(box).not.toBeDisabled()
    expect(box).toHaveAttribute('aria-disabled', 'true')
    box.focus()
    expect(box).toHaveFocus()

    await userEvent.click(box)

    expect(onToggle).not.toHaveBeenCalled()
    expect(toast.info).toHaveBeenCalledWith('Completion counts unavailable — reload to retry.', {
      id: 'task-counts-unavailable',
    })
  })

  it('disables the box while that task’s toggle is in flight', () => {
    renderRollupRail(PARTIAL, { togglingTaskIds: new Set([TASK_ID]) })

    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('shows the roll-up alone when no conversation is the caller’s', () => {
    renderRollupRail(PARTIAL.map((m) => ({ ...m, canToggle: false })))

    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByLabelText('Completed for 1 of 2 models')).toHaveTextContent('1/2')
    expect(screen.getByText('Read-only — not your conversation.')).toBeInTheDocument()
  })

  it('shows no roll-up at all when the caller may not read the counts', () => {
    renderRollupRail(
      PARTIAL.map((m) => ({ ...m, canToggle: false })),
      { countsReadable: false },
    )

    // The reads a supervisor can make are author-scoped and so structurally empty; a badge
    // off them would report the owner's work as 0/2.
    expect(screen.queryByLabelText(/Completed for/)).not.toBeInTheDocument()
    expect(screen.queryByText('1/2')).not.toBeInTheDocument()
    // Not an indeterminate bar either — that still promises a number is coming.
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText(task.name)).toBeInTheDocument()
    // The missing counts are the more surprising absence, and the rail shows a single note.
    expect(
      screen.getByText("Completion progress isn't shown for conversations you don't own."),
    ).toBeInTheDocument()
    expect(screen.queryByText('Read-only — not your conversation.')).not.toBeInTheDocument()
  })

  it('keeps the owned box true and operable in a mixed group with no roll-up', async () => {
    const { onToggle } = renderRollupRail(
      [
        member({ conversationId: 'c1', completedTaskIds: done() }),
        member({ conversationId: 'c2', canToggle: false }),
      ],
      { countsReadable: false },
    )

    const box = screen.getByRole('checkbox')
    expect(box).toBeChecked()
    expect(box).not.toBeDisabled()
    await userEvent.click(box)
    expect(onToggle).toHaveBeenCalledWith(TASK_ID, false)
    expect(screen.queryByLabelText(/Completed for/)).not.toBeInTheDocument()
  })

  it('still disables the owned box while its read is in flight, roll-up hidden or not', () => {
    renderRollupRail(MIXED, { countsReadable: false, countsState: 'loading' })

    // An enabled box reading unchecked would assert "not completed" from a set still in flight.
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.queryByLabelText('Loading completion counts')).not.toBeInTheDocument()
  })

  it('blames a failed read rather than ownership when the roll-up is unreadable', async () => {
    const { onToggle } = renderRollupRail(MIXED, { countsReadable: false, countsState: 'error' })
    const box = screen.getByRole('checkbox')

    expect(box).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(box)
    expect(onToggle).not.toHaveBeenCalled()
    // A retryable failure outranks the privacy note — attributing it to ownership would send
    // the caller away from the one thing that would fix it.
    expect(screen.getByText('Completion counts unavailable — reload to retry.')).toBeInTheDocument()
    expect(
      screen.queryByText("Completion progress isn't shown for conversations you don't own."),
    ).not.toBeInTheDocument()
  })

  it('renders an empty group as 0/0 with nothing to check', () => {
    renderRollupRail([])

    expect(screen.getByLabelText('Completed for 0 of 0 models')).toHaveTextContent('0/0')
    // No members means no checkbox and no read-only claim — the group is empty, not blocked.
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.queryByText('Read-only — not your conversation.')).not.toBeInTheDocument()
  })
})
