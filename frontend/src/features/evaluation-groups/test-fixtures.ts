import type { EvaluationGroupMetricsResponse } from '@/lib/api/types'

// One group-metrics fixture for every suite that needs one, so a contract change breaks in a
// single place instead of drifting across hand-written literals. Note `members.by_role` is a
// record, not a list — a re-guessed stub gets that wrong.
export function groupMetricsStub(
  overrides: Partial<EvaluationGroupMetricsResponse> = {},
): EvaluationGroupMetricsResponse {
  return {
    scope: 'full',
    group_id: 'grp-0001-0000-0000-000000000000',
    members: { total: 3, by_role: { owner: 1, red_teamer: 2 } },
    submissions: { total: 5, pending: 2, approved: 2, rejected: 1 },
    reviews: {
      total: 4,
      completed: 3,
      pending: 1,
      successful_exploit: 2,
      unique_exploit: 1,
      valid_submission: 3,
    },
    activity: { conversations: 6, messages: 42 },
    tokens: {
      prompt_tokens: 900,
      completion_tokens: 300,
      total_tokens: 1200,
      messages_with_usage: 20,
      conversations_with_usage: 6,
      avg_tokens_per_message: 60,
      avg_tokens_per_conversation: 200,
    },
    tokens_by_model_withheld: false,
    tokens_by_model: [
      {
        ai_model_id: 'm1',
        model_alias: 'gpt-4o-mini',
        tokens: {
          prompt_tokens: 900,
          completion_tokens: 300,
          total_tokens: 1200,
          messages_with_usage: 20,
          conversations_with_usage: 6,
          avg_tokens_per_message: 60,
          avg_tokens_per_conversation: 200,
        },
      },
    ],
    evaluations: [
      {
        evaluation_id: 'e1',
        title: 'Prompt injection',
        models_assigned: 2,
        submissions: { total: 5, pending: 2, approved: 2, rejected: 1 },
        reviews: {
          total: 4,
          completed: 3,
          pending: 1,
          successful_exploit: 2,
          unique_exploit: 1,
          valid_submission: 3,
        },
        activity: { conversations: 6, messages: 42 },
        tokens: {
          prompt_tokens: 900,
          completion_tokens: 300,
          total_tokens: 1200,
          messages_with_usage: 20,
          conversations_with_usage: 6,
          avg_tokens_per_message: 60,
          avg_tokens_per_conversation: 200,
        },
        scenarios: [
          {
            scenario_id: 's1',
            name: 'System-prompt leak',
            submissions_total: 3,
            tasks: [{ task_id: 't1', name: 'Extract instructions', submissions_total: 2 }],
            tokens: {
              prompt_tokens: 450,
              completion_tokens: 150,
              total_tokens: 600,
              messages_with_usage: 10,
              conversations_with_usage: 3,
              avg_tokens_per_message: 60,
              avg_tokens_per_conversation: 200,
            },
          },
        ],
        // Sparse — only days that carry a submission, hence the distinct field name.
        submissions_by_active_day: [
          { day: '2026-08-03', submissions: 3, exploited_submissions: 1 },
          { day: '2026-08-05', submissions: 2, exploited_submissions: 1 },
        ],
      },
    ],
    // Dense — the shared axis the per-evaluation series are plotted against.
    submissions_by_day: [
      { day: '2026-08-03', submissions: 3, exploited_submissions: 1 },
      { day: '2026-08-04', submissions: 0, exploited_submissions: 0 },
      { day: '2026-08-05', submissions: 2, exploited_submissions: 1 },
    ],
    ...overrides,
  }
}
