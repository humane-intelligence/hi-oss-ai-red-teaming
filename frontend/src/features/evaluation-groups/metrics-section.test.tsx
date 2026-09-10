import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  ActivityCard,
  EvaluationMetricsDetail,
  ReviewsCard,
  SubmissionsCard,
  GroupTokensByModelCard,
  TokensCard,
} from './metrics-section'
import type { EvaluationMetrics } from '@/lib/api/types'
import { groupMetricsStub } from './test-fixtures'

const metrics = groupMetricsStub()
const evaluation: EvaluationMetrics = metrics.evaluations[0]!

describe('ActivityCard derived ratios', () => {
  it('shows how deep conversations run and how often they yield a submission', () => {
    render(
      <ActivityCard
        activity={{ conversations: 6, messages: 42 }}
        submissions={metrics.submissions}
      />,
    )
    expect(screen.getByText('Messages / conv')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('Submissions / conv')).toBeInTheDocument()
    // 5 submissions / 6 conversations -> 0.8, not a rounded-away 1.
    expect(screen.getByText('0.8')).toBeInTheDocument()
  })

  it('reads unmeasured, not zero, when there are no conversations', () => {
    // A ratio with an empty denominator is not "0 per conversation" — printing a confident zero
    // would claim a measurement nobody made.
    render(
      <ActivityCard
        activity={{ conversations: 0, messages: 0 }}
        submissions={metrics.submissions}
      />,
    )
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })
})

describe('metrics building blocks', () => {
  it('SubmissionsCard shows the total and status breakdown', () => {
    render(<SubmissionsCard submissions={metrics.submissions} />)
    expect(screen.getByText('Total')).toBeInTheDocument()
    expect(screen.getByText('Approved')).toBeInTheDocument()
  })

  it('ReviewsCard renders the verdict tally', () => {
    render(<ReviewsCard reviews={metrics.reviews} />)
    expect(screen.getByText('Successful exploit')).toBeInTheDocument()
    expect(screen.getByText('Unique exploit')).toBeInTheDocument()
  })

  it('ActivityCard renders conversation and message counts', () => {
    render(<ActivityCard activity={metrics.activity} submissions={metrics.submissions} />)
    expect(screen.getByText('Conversations')).toBeInTheDocument()
    expect(screen.getByText('Messages')).toBeInTheDocument()
  })

  it('EvaluationMetricsDetail renders per-evaluation scenario/task counts', () => {
    render(<EvaluationMetricsDetail evaluation={evaluation} />)
    expect(screen.getByText('System-prompt leak')).toBeInTheDocument()
    expect(screen.getByText(/Extract instructions/)).toBeInTheDocument()
  })

  it('TokensCard shows the spend split as shares of the total', () => {
    render(<TokensCard tokens={metrics.tokens} />)

    expect(screen.getByText('1,200')).toBeInTheDocument()
    // Input and output are parts of that total, so each is shown with its share — 900 and 300 of
    // 1,200. Bare amounts alone would leave the reader summing them to check.
    expect(screen.getByText('900')).toBeInTheDocument()
    expect(screen.getByText(/75%/)).toBeInTheDocument()
    expect(screen.getByText('300')).toBeInTheDocument()
    expect(screen.getByText(/25%/)).toBeInTheDocument()
    expect(screen.getByText('Input (prompts)')).toBeInTheDocument()
    expect(screen.getByText('Output (replies)')).toBeInTheDocument()
  })

  it('TokensCard names both denominators the averages divide by', () => {
    render(<TokensCard tokens={metrics.tokens} />)

    // The averages are only interpretable next to the counts they divide by: a provider reports
    // usage on completed replies only, so "per reply" is not "per message" and the card must not
    // let the two be confused.
    expect(screen.getByText('Average per reply')).toBeInTheDocument()
    expect(screen.getByText('Average per conversation')).toBeInTheDocument()
    expect(
      screen.getByText(/Measured on 20 replies across 6 conversations that reported usage/),
    ).toBeInTheDocument()
  })

  it('TokensCard explains that a prompt re-counts the whole conversation so far', () => {
    render(<TokensCard tokens={metrics.tokens} />)

    // The one accounting fact no label can carry: input grows with conversation depth because
    // every turn resends the history. Without it, "Input 900" reads as the size of one prompt.
    expect(screen.getByLabelText(/resends the whole conversation so far/)).toBeInTheDocument()
  })

  it('TokensCard drops the split bar when the provider reported no breakdown', () => {
    // A provider may report `total_tokens` alone. Drawing an empty bar beside a non-zero total
    // would read as "all input, no output" instead of "breakdown not reported".
    render(
      <TokensCard
        tokens={{
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 450,
          messages_with_usage: 3,
          conversations_with_usage: 2,
          avg_tokens_per_message: 150,
          avg_tokens_per_conversation: 225,
        }}
      />,
    )

    expect(screen.getByText('450')).toBeInTheDocument()
    expect(
      screen.getByText(/reported a total without an input\/output breakdown/),
    ).toBeInTheDocument()
    expect(screen.queryByText('Input (prompts)')).toBeNull()
  })

  it('TokensCard says nothing was reported rather than rendering zeros', () => {
    // The credential-less case: a model that answered only with errors reports no usage at all.
    // Zeros here would read as "free"; the copy has to say the data is absent instead.
    render(
      <TokensCard
        tokens={{
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
          messages_with_usage: 0,
          conversations_with_usage: 0,
          avg_tokens_per_message: 0,
          avg_tokens_per_conversation: 0,
        }}
      />,
    )

    expect(screen.getByText(/Providers reported no tokens for these replies/)).toBeInTheDocument()
    // Not merely "no numbers": none of the spend furniture may appear, or a zero average would
    // still be sitting there implying the event was free.
    expect(screen.queryByText('tokens total')).toBeNull()
    expect(screen.queryByText('Input (prompts)')).toBeNull()
    expect(screen.queryByText('Average per reply')).toBeNull()
  })

  it('TokensCard says nothing was reported when the total is zero despite a usage-bearing count', () => {
    // A legitimately reported 0 is accepted, so the count can be non-zero while the total is 0.
    // Gating on the count alone printed "0 tokens total" and a breakdown note — both false.
    render(
      <TokensCard
        tokens={{
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
          messages_with_usage: 4,
          conversations_with_usage: 2,
          avg_tokens_per_message: 0,
          avg_tokens_per_conversation: 0,
        }}
      />,
    )

    expect(screen.getByText(/Providers reported no tokens for these replies/)).toBeInTheDocument()
    expect(screen.queryByText('tokens total')).toBeNull()
    expect(screen.queryByText(/reported a total without an input\/output breakdown/)).toBeNull()
  })

  it('GroupTokensByModelCard lists per-model spend across the event', () => {
    render(
      <GroupTokensByModelCard
        tokensByModel={metrics.tokens_by_model}
        withheld={metrics.tokens_by_model_withheld}
      />,
    )

    expect(screen.getByText('Tokens by model')).toBeInTheDocument()
    expect(screen.getByText('gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText('1,200')).toBeInTheDocument()
  })

  it('GroupTokensByModelCard says the attribution is hidden, not absent, when models exist', () => {
    // An empty list is ambiguous on its own, so the backend states which case it is
    // (`tokens_by_model_withheld`) rather than leaving each client to infer it from assignment
    // counts. Telling a member of a two-model event that no models are assigned would be a plain
    // falsehood — and inferring it was also wrong for a masking group with nothing assigned yet.
    render(<GroupTokensByModelCard tokensByModel={[]} withheld />)

    expect(
      screen.getByText(/hidden because an evaluation in this group masks model names/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/No models assigned/)).toBeNull()
  })

  it('GroupTokensByModelCard falls back to the empty state when nothing is assigned', () => {
    render(<GroupTokensByModelCard tokensByModel={[]} withheld={false} />)

    expect(screen.getByText(/No models assigned yet/)).toBeInTheDocument()
  })

  it('EvaluationMetricsDetail says none reported when the total is zero', () => {
    // The per-evaluation detail has its own gate, and the same accepted-zero state reaches it.
    render(
      <EvaluationMetricsDetail
        evaluation={{
          ...evaluation,
          tokens: { ...evaluation.tokens, total_tokens: 0, messages_with_usage: 4 },
        }}
      />,
    )

    expect(screen.getByText('none reported')).toBeInTheDocument()
    expect(screen.queryByText(/per reply/)).toBeNull()
  })

  it('EvaluationMetricsDetail surfaces token spend per scenario', () => {
    render(<EvaluationMetricsDetail evaluation={evaluation} />)

    // The evaluation cell and the scenario row carry their own totals.
    expect(screen.getByText('1,200')).toBeInTheDocument()
    expect(screen.getByText('600')).toBeInTheDocument()
  })
})
