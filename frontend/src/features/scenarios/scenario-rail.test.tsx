import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { ScenarioRail } from './scenario-rail'
import { togglingKey } from './task-completion-ui'
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
  name,
  ...overrides
}: Partial<RailMember> & { conversationId: string; name?: string }): RailMember {
  // The rail is handed resolved labels (the group page builds them via `member-names`), so
  // a case only needs the name it wants rendered.
  const label = name ?? `model-${conversationId}`
  return {
    conversationId,
    label,
    a11yLabel: label,
    completedTaskIds: new Set(),
    canToggle: true,
    ...overrides,
  }
}

function renderRollupRail(
  members: RailMember[],
  opts?: {
    countsState?: RollupCountsState
    countsReadable?: boolean
    togglingKeys?: Set<string>
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
        togglingKeys: opts?.togglingKeys ?? new Set(),
        countsState: opts?.countsState ?? 'ready',
        countsReadable: opts?.countsReadable ?? true,
      }}
    />,
  )
  return { onToggle }
}

describe('ScenarioRail rollup mode', () => {
  const MEMBERS = [
    member({ conversationId: 'c1', name: 'gpt-4o', completedTaskIds: new Set([TASK_ID]) }),
    member({ conversationId: 'c2', name: 'claude-sonnet-4' }),
  ]
  // A group a break-glass manager composed: the caller owns c1, someone else owns c2.
  const MIXED = [
    member({ conversationId: 'c1', name: 'gpt-4o' }),
    member({ conversationId: 'c2', name: 'claude-sonnet-4', canToggle: false }),
  ]

  it('shows the K/N roll-up collapsed, with no checkbox until expanded', () => {
    renderRollupRail(MEMBERS)

    expect(screen.getByLabelText('Completed in 1 of 2 conversations')).toHaveTextContent('1/2')
    expect(screen.getByRole('button', { name: /Basic injection/ })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('expands into one checkbox per owned member, reflecting each conversation’s state', async () => {
    renderRollupRail(MEMBERS)

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    expect(boxes[0]).toBeChecked() // gpt-4o has the task checked off
    expect(boxes[1]).not.toBeChecked()
    expect(screen.getByText('gpt-4o')).toBeInTheDocument()
    expect(screen.getByText('claude-sonnet-4')).toBeInTheDocument()
  })

  it('toggles the task for the member whose checkbox was clicked', async () => {
    const { onToggle } = renderRollupRail(MEMBERS)

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))
    await userEvent.click(screen.getAllByRole('checkbox')[1]!)

    expect(onToggle).toHaveBeenCalledWith('c2', TASK_ID, true)
  })

  it('derives the roll-up from the members, counting a task done only when all have it', () => {
    renderRollupRail([
      member({ conversationId: 'c1', completedTaskIds: new Set([TASK_ID]) }),
      member({ conversationId: 'c2', completedTaskIds: new Set([TASK_ID]) }),
    ])

    expect(screen.getByLabelText('Completed in 2 of 2 conversations')).toHaveTextContent('2/2')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
  })

  it('shows the task rows while a member’s completions load, without asserting a count', () => {
    renderRollupRail(MEMBERS, { countsState: 'loading' })

    // The block must NOT unmount: adding a model mounts a new query, and dropping the
    // rows would collapse every open disclosure.
    expect(screen.getByText(task.name)).toBeInTheDocument()
    expect(screen.getByLabelText('Loading completion counts')).toHaveTextContent('…')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
  })

  it('says the counts are unavailable when a member’s read failed', () => {
    renderRollupRail(MEMBERS, { countsState: 'error' })

    // Silently reading a failed member as "nothing completed" would under-report K.
    expect(screen.getByLabelText('Completion counts unavailable')).toHaveTextContent('—')
    // Inline too, not only on activation — a sighted mouse user must not have to click a
    // seemingly-live checkbox to find out why nothing happens.
    expect(screen.getByText('Completion counts unavailable — reload to retry.')).toBeInTheDocument()
    // Indeterminate: omitted, not 0 — which would assert "0% done".
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
  })

  it('leaves a row reachable and explains itself when the counts failed', async () => {
    const { onToggle } = renderRollupRail(MEMBERS, { countsState: 'error' })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))
    const box = screen.getAllByRole('checkbox')[0]!

    // A failed read doesn't recover on its own (retry: 1, no refetch on focus), so a real
    // `disabled` would leave a permanently dead control out of the tab order — the exact
    // pattern the blocked-row half of this work removed.
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

  it('keeps a real disabled while the counts are merely loading', async () => {
    renderRollupRail(MEMBERS, { countsState: 'loading' })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    // Transient and self-clearing, so there is nothing to explain.
    expect(screen.getAllByRole('checkbox')[0]!).toBeDisabled()
  })

  it('drops the disclosure entirely when no conversation is the caller’s', () => {
    renderRollupRail(
      [
        member({ conversationId: 'c1', name: 'gpt-4o', completedTaskIds: new Set([TASK_ID]) }),
        member({ conversationId: 'c2', name: 'claude-sonnet-4' }),
      ].map((m) => ({ ...m, canToggle: false })),
    )

    // The per-member breakdown exists to be acted on; with nothing actionable the row is
    // just the roll-up, as this view showed before check-off arrived.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByLabelText('Completed in 1 of 2 conversations')).toHaveTextContent('1/2')
    expect(screen.getByText('Read-only — not your conversations.')).toBeInTheDocument()
  })

  it('shows no roll-up at all when the caller may not read the counts', () => {
    renderRollupRail(
      [
        member({ conversationId: 'c1', name: 'gpt-4o', completedTaskIds: new Set([TASK_ID]) }),
        member({ conversationId: 'c2', name: 'claude-sonnet-4' }),
      ].map((m) => ({ ...m, canToggle: false })),
      { countsReadable: false },
    )

    // The reads a supervisor can make are author-scoped and so structurally empty; a badge
    // off them would report the owner's work as 0/2.
    expect(screen.queryByLabelText(/Completed in/)).not.toBeInTheDocument()
    expect(screen.queryByText('1/2')).not.toBeInTheDocument()
    // Not an indeterminate bar either — that still promises a number is coming.
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    // The checklist itself stays: the tasks are worth reading even without a score.
    expect(screen.getByText(task.name)).toBeInTheDocument()
    expect(
      screen.getByText("Completion progress isn't shown for conversations you don't own."),
    ).toBeInTheDocument()
  })

  it('explains the missing counts rather than the read-only rows', () => {
    renderRollupRail([member({ conversationId: 'c1', canToggle: false })], {
      countsReadable: false,
    })

    // Both notes are true, but the absent roll-up is the more surprising one, and the rail
    // shows a single note.
    expect(
      screen.getByText("Completion progress isn't shown for conversations you don't own."),
    ).toBeInTheDocument()
    expect(screen.queryByText('Read-only — not your conversations.')).not.toBeInTheDocument()
  })

  it('keeps an owned row true and operable in a mixed group with no roll-up', async () => {
    // The caller owns c1 of a group a break-glass manager composed. Hiding the group's count
    // must not cost them their own row: its state is readable and its checkbox must work.
    const { onToggle } = renderRollupRail(
      [
        member({ conversationId: 'c1', name: 'gpt-4o', completedTaskIds: new Set([TASK_ID]) }),
        member({ conversationId: 'c2', name: 'claude-sonnet-4', canToggle: false }),
      ],
      { countsReadable: false },
    )

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    const box = screen.getByRole('checkbox')
    expect(box).toBeChecked()
    expect(box).not.toBeDisabled()
    await userEvent.click(box)
    expect(onToggle).toHaveBeenCalledWith('c1', TASK_ID, false)
    // Still no group number — c2's completions remain unreadable.
    expect(screen.queryByLabelText(/Completed in/)).not.toBeInTheDocument()
  })

  it('still disables an owned row while its read is in flight, roll-up hidden or not', async () => {
    renderRollupRail(MIXED, { countsReadable: false, countsState: 'loading' })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    // Hiding the group number must not cost the caller the load signal on their own row — an
    // enabled box reading unchecked asserts "not completed" from a set still in flight.
    expect(screen.getByRole('checkbox')).toBeDisabled()
    // Named, not `/Completed in/`: under `loading` the badge is labelled "Loading completion
    // counts", so the loose pattern passes whether or not the badge renders.
    expect(screen.queryByLabelText('Loading completion counts')).not.toBeInTheDocument()
  })

  it('reports an unknown state on a non-owned row when the roll-up is unreadable', async () => {
    renderRollupRail(MIXED, { countsReadable: false })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    // Settled reads, but not the caller's to make: "not completed" here would be the hidden
    // roll-up's claim restated one row down.
    expect(screen.getByLabelText(/claude-sonnet-4 — completion unknown$/)).toBeInTheDocument()
  })

  it('still explains a failed read on an owned row, and blames the failure not ownership', async () => {
    const { onToggle } = renderRollupRail(MIXED, { countsReadable: false, countsState: 'error' })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))
    const box = screen.getByRole('checkbox')

    expect(box).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(box)
    expect(onToggle).not.toHaveBeenCalled()
    expect(toast.info).toHaveBeenCalledWith('Completion counts unavailable — reload to retry.', {
      id: 'task-counts-unavailable',
    })
    // A retryable failure outranks the privacy note — attributing it to ownership would send
    // the caller away from the one thing that would fix it.
    expect(screen.getByText('Completion counts unavailable — reload to retry.')).toBeInTheDocument()
    expect(
      screen.queryByText("Completion progress isn't shown for conversations you don't own."),
    ).not.toBeInTheDocument()
  })

  it('shows state but no checkbox on the members of a mixed group it cannot write to', async () => {
    const { onToggle } = renderRollupRail([
      member({ conversationId: 'c1', name: 'gpt-4o' }),
      member({
        conversationId: 'c2',
        name: 'claude-sonnet-4',
        completedTaskIds: new Set([TASK_ID]),
        canToggle: false,
      }),
    ])

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    // An affordance that can only ever refuse is worse than none — the blocked member's
    // state is conveyed by a static marker instead.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    expect(screen.getByLabelText(/claude-sonnet-4 — completed$/)).toBeInTheDocument()
    expect(onToggle).not.toHaveBeenCalled()
    // "Read-only" would be a lie with a live checkbox on screen.
    expect(screen.getByText("Conversations you don't own show state only.")).toBeInTheDocument()
    expect(screen.queryByText('Read-only — not your conversations.')).not.toBeInTheDocument()
  })

  it('reports an unknown state on a non-owned row while the counts are failed', async () => {
    renderRollupRail(
      [
        member({ conversationId: 'c1', name: 'gpt-4o' }),
        member({ conversationId: 'c2', name: 'claude-sonnet-4', canToggle: false }),
      ],
      { countsState: 'error' },
    )

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    // The marker must not assert "not completed" from a set that never resolved — the
    // same rule the badge and the checkbox already follow.
    expect(screen.getByLabelText(/claude-sonnet-4 — completion unknown$/)).toBeInTheDocument()
  })

  it('disables only the member row whose toggle is in flight', async () => {
    renderRollupRail(MEMBERS, { togglingKeys: new Set([togglingKey('c1', TASK_ID)]) })

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    const boxes = screen.getAllByRole('checkbox')
    expect(boxes[0]).toBeDisabled()
    expect(boxes[1]).not.toBeDisabled()
  })

  it('renders the resolved label it is given, wherever the name appears', async () => {
    // Building the label — title vs model, and the position suffix that disambiguates a
    // masked group — belongs to `member-names` (see member-names.test.ts). The rail's job
    // is to render what it's handed, in the row, the tooltip and the accessible name.
    renderRollupRail([
      member({ conversationId: 'c1', name: '— masked — (1)', a11yLabel: '— masked — (1)' }),
      member({
        conversationId: 'c2',
        name: 'Refusal probe (2)',
        a11yLabel: 'Refusal probe (2) (gpt-4o)',
      }),
    ])

    await userEvent.click(screen.getByRole('button', { name: /Basic injection/ }))

    expect(screen.getByText('— masked — (1)')).toBeInTheDocument()
    expect(screen.getByText('Refusal probe (2)')).toBeInTheDocument()
    expect(
      screen.getByRole('checkbox', { name: 'Basic injection — Refusal probe (2) (gpt-4o)' }),
    ).toBeInTheDocument()
  })

  it('renders an empty group as 0/0 with nothing to expand', () => {
    renderRollupRail([])

    expect(screen.getByLabelText('Completed in 0 of 0 conversations')).toHaveTextContent('0/0')
    // No members means no panel and no read-only claim — the group is empty, not blocked.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByText('Read-only — not your conversations.')).not.toBeInTheDocument()
  })
})
