import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GroupMetricsTab } from './group-metrics-tab'
import { groupMetricsStub } from './test-fixtures'

const metrics = groupMetricsStub()
const rollUpOnly = groupMetricsStub({ evaluations: [] })

describe('GroupMetricsTab', () => {
  // Without evaluations, so the card titles are unambiguous: `EvaluationMetricsDetail` labels its
  // own breakdown with the same words.
  it('renders the roll-up cards', () => {
    render(<GroupMetricsTab data={rollUpOnly} isError={false} error={null} />)
    expect(screen.getByText('Submissions')).toBeInTheDocument()
    expect(screen.getByText('Reviews & exploits')).toBeInTheDocument()
    expect(screen.getByText('Activity')).toBeInTheDocument()
    expect(screen.getByText('Tokens')).toBeInTheDocument()
  })

  it('shows the per-evaluation breakdown', () => {
    render(<GroupMetricsTab data={metrics} isError={false} error={null} />)
    expect(screen.getByText('Prompt injection')).toBeInTheDocument()
  })

  it('surfaces the personal-scope badge, which is hidden on a full read', () => {
    render(
      <GroupMetricsTab
        data={groupMetricsStub({ scope: 'personal' })}
        isError={false}
        error={null}
      />,
    )
    expect(screen.getByText(/only your own data/i)).toBeInTheDocument()
  })

  it('shows the error copy only when there is no data', () => {
    render(<GroupMetricsTab data={undefined} isError error={new Error('boom')} />)
    expect(screen.getByText(/boom|went wrong|Request failed/i)).toBeInTheDocument()
  })

  it('keeps last-good data when a background refetch failed', () => {
    render(<GroupMetricsTab data={rollUpOnly} isError error={new Error('boom')} />)
    // Data present wins: the cards stay and no destructive card stacks on top of live numbers.
    expect(screen.getByText('Reviews & exploits')).toBeInTheDocument()
    expect(screen.queryByText(/boom/i)).toBeNull()
  })

  it("captions each evaluation with its own totals over the group's window", () => {
    const base = groupMetricsStub()
    // The stub's group and its single evaluation happen to total the same, which would let a caption
    // wired to the wrong series pass — so give the evaluation a series of its own.
    const data = {
      ...base,
      evaluations: [
        {
          ...base.evaluations[0]!,
          submissions_by_active_day: [
            { day: '2026-08-05', submissions: 2, exploited_submissions: 1 },
          ],
        },
      ],
    }
    render(<GroupMetricsTab data={data} isError={false} error={null} />)
    // Same window on both — the sparkline is densified against the group's dense axis — but its own
    // totals, not the group's 5 and 2.
    expect(screen.getByText('3 Aug – 5 Aug · 5 submissions, 2 exploited')).toBeInTheDocument()
    expect(screen.getByText('3 Aug – 5 Aug · 2 submissions, 1 exploited')).toBeInTheDocument()
  })

  it('says nothing is plottable when the series is empty', () => {
    render(
      <GroupMetricsTab
        data={groupMetricsStub({ submissions_by_day: [] })}
        isError={false}
        error={null}
      />,
    )
    expect(screen.getByText(/nothing to plot yet/i)).toBeInTheDocument()
  })
})
