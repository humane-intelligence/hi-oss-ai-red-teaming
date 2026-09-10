import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { EvaluationDetailPage } from './evaluation-detail-page'
import type { EvaluationMetricsResponse, EvaluationResponse } from '@/lib/api/types'
import { licenseStub } from '@/features/licenses/test-fixtures'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'

const baseEvaluation: EvaluationResponse = {
  id: EVAL_ID,
  evaluation_group_id: 'group-0001-0000-0000-000000000000',
  created_by_id: 'user-0001',
  title: 'Test evaluation',
  description: 'A test evaluation',
  status: 'draft',
  mask_models_enabled: false,
  tags_enabled: true,
  tags_restricted: false,
  effective_license: licenseStub('CC-BY-4.0'),
  models: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const underReviewEvaluation: EvaluationResponse = {
  ...baseEvaluation,
  status: 'under_review',
}

const GROUP_ID = baseEvaluation.evaluation_group_id

// MSW handlers for all sub-queries the page fires
function baseHandlers(
  evaluation: EvaluationResponse = baseEvaluation,
  groupTitle = 'Spring jailbreak',
) {
  return [
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () => HttpResponse.json(evaluation)),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups`, () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    // AssignModelDialog always mounts and queries ai-models
    http.get('http://localhost/api/v1/ai-models', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    // AllowedTagsCard mounts in the aside and queries the evaluation's tag keys.
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () =>
      HttpResponse.json([]),
    ),
    // group lookup for breadcrumbs + Group field. Status must accept evaluations
    // (approved/published) or the Duplicate entry point hides itself.
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
      HttpResponse.json({
        id: GROUP_ID,
        title: groupTitle,
        description: '',
        status: 'approved',
        access_level: 'open',
        start_date: '2026-01-01',
        end_date: null,
        created_by_id: 'user-0001',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        evaluations: [],
      }),
    ),
  ]
}

function renderPage(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/evaluations/${EVAL_ID}`]}>
          <Routes>
            <Route path="/evaluations/:id" element={<EvaluationDetailPage />} />
            <Route path="/evaluations/:id/edit" element={<div>EDIT PAGE</div>} />
            <Route path="/evaluations" element={<div>Evaluations list</div>} />
            <Route path="/evaluation-groups" element={<div>Evaluation groups list</div>} />
            <Route path="/evaluation-groups/:id" element={<div>Group detail</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('EvaluationDetailPage — group link', () => {
  it('shows the group title as a link (not raw UUID) once group loads', async () => {
    server.use(...baseHandlers(baseEvaluation, 'Spring jailbreak'))
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await waitFor(() => {
      const links = screen.getAllByRole('link', { name: /spring jailbreak/i })
      expect(links.length).toBeGreaterThanOrEqual(1)
    })
  })
})

describe('EvaluationDetailPage — the two navigations', () => {
  it('says where the page sits once, and says it in the identity line', async () => {
    server.use(...baseHandlers(baseEvaluation, 'Spring jailbreak'))
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    const identity = await screen.findByRole('navigation', { name: 'Evaluation group' })
    expect(within(identity).getByRole('link', { name: 'Evaluation Groups' })).toBeInTheDocument()
    expect(
      await within(identity).findByRole('link', { name: /spring jailbreak/i }),
    ).toBeInTheDocument()

    // No second trail: it could only carry the group this line ends with and the evaluation the
    // heading below states, so it printed both twice. (The meta line under the heading still links
    // the group beside its author — pre-existing, and the only remaining repeat.)
    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull()
    expect(screen.getByRole('heading', { level: 1, name: 'Test evaluation' })).toBeInTheDocument()
    expect(within(identity).getAllByRole('link', { name: /spring jailbreak/i })).toHaveLength(1)
  })
})

describe('EvaluationDetailPage — RBAC gates', () => {
  it('with evaluations:read only — no Edit, Approve, or Reject buttons', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reject/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /assign model/i })).toBeNull()
  })

  it('with evaluations:update + models:read — Edit and Assign model buttons are visible', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read', 'evaluations:update', 'models:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /assign model/i })).toBeInTheDocument()
  })

  it('with evaluations:update but no models:read — Assign model hidden, page Edit still visible', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read', 'evaluations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /assign model/i })).toBeNull()
  })

  it('with evaluations:approve + under_review — Approve and Reject visible', async () => {
    server.use(...baseHandlers(underReviewEvaluation))
    renderPage(['evaluations:read', 'evaluations:approve', 'evaluations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /^approve$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^reject$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
  })

  it('with evaluations:approve but status=draft — no Approve or Reject', async () => {
    server.use(...baseHandlers(baseEvaluation))
    renderPage(['evaluations:read', 'evaluations:approve'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^reject$/i })).toBeNull()
  })
})

describe('EvaluationDetailPage — duplicate', () => {
  it('opens a dialog, sends include_children when checked, and navigates to the copy edit page', async () => {
    let includeChildren: string | null = null
    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/duplicate`, ({ request }) => {
        includeChildren = new URL(request.url).searchParams.get('include_children')
        return HttpResponse.json(
          { ...baseEvaluation, id: 'dup-id', status: 'new' },
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    renderPage([
      'evaluations:read',
      'evaluations:create',
      'evaluations:update',
      'evaluation_groups:read',
    ])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /duplicate/i }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('checkbox'))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /duplicate/i }))

    await waitFor(() => expect(includeChildren).toBe('true'))
    await waitFor(() => expect(screen.getByText('EDIT PAGE')).toBeInTheDocument())
  })

  it('falls back to the copy detail page without evaluations:update', async () => {
    const copy = {
      ...baseEvaluation,
      id: 'dup-id',
      title: 'Test evaluation (copy)',
      status: 'new' as const,
    }
    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/duplicate`, () =>
        HttpResponse.json(copy, { status: 201 }),
      ),
      http.get('http://localhost/api/v1/evaluations/dup-id', () => HttpResponse.json(copy)),
      http.get('http://localhost/api/v1/evaluations/dup-id/scenarios', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/evaluations/dup-id/conversation-groups', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    const user = userEvent.setup()
    renderPage(['evaluations:read', 'evaluations:create', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /duplicate/i }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: /duplicate/i }),
    )

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation (copy)' })).toBeInTheDocument(),
    )
  })

  it('is hidden without evaluations:create', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /duplicate/i })).toBeNull()
  })

  it('is hidden when the parent group does not accept evaluations', async () => {
    // The copy lands in the parent group, and the API 409s adds to a non-approved
    // group — so even a caller with evaluations:create gets no Duplicate there.
    // Override first — MSW uses the first matching handler.
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Draft group',
          description: '',
          status: 'draft',
          access_level: 'open',
          start_date: '2026-01-01',
          end_date: null,
          created_by_id: 'user-0001',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
        }),
      ),
      ...baseHandlers(),
    )
    renderPage(['evaluations:read', 'evaluations:create', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getAllByText(/draft group/i).length).toBeGreaterThanOrEqual(1),
    )
    expect(screen.queryByRole('button', { name: /duplicate/i })).toBeNull()
  })
})

const ASSIGN_ID = 'asgn-0001-0000-0000-000000000000'

const evaluationWithModel: EvaluationResponse = {
  ...baseEvaluation,
  models: [
    {
      assignment_id: ASSIGN_ID,
      name: 'gpt-4o',
      provider: 'openai',
      provider_model_id: 'gpt-4o',
      warmup_enabled: false,
      advanced_params_disabled: false,
      input_modalities: ['text'],
    },
  ],
}

describe('EvaluationDetailPage — edit model parameters', () => {
  it('opens the edit dialog prefilled with the assignment mask and params', async () => {
    server.use(
      ...baseHandlers(evaluationWithModel),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json({
          id: ASSIGN_ID,
          evaluation_id: EVAL_ID,
          model_id: 'mdl-0001-0000-0000-000000000000',
          model_display_mask: 'Model A',
          parameters: { temperature: 0.3 },
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      ),
    )
    const user = userEvent.setup()
    renderPage(['evaluations:read', 'evaluations:update', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /edit model parameters/i }))

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /edit model parameters/i })).toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getByLabelText('Display mask (optional)')).toHaveValue('Model A'),
    )
    expect(screen.getByLabelText('Temperature')).toHaveValue(0.3)
  })
})

describe('EvaluationDetailPage — data license', () => {
  // Handlers for an evaluation with no override whose parent group sets the license,
  // so the effective license is inherited from the group (not the platform default).
  function inheritedHandlers() {
    return [
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}`, () =>
        HttpResponse.json({
          ...baseEvaluation,
          data_license_id: null,
          effective_license: licenseStub('CC0-1.0'),
        }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/conversation-groups`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json({
          id: GROUP_ID,
          title: 'Host group',
          description: '',
          status: 'draft',
          access_level: 'public',
          start_date: '2026-01-01',
          data_license_id: licenseStub('CC0-1.0').id,
          effective_license: licenseStub('CC0-1.0'),
          created_by_id: 'user-0001',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          evaluations: [],
        }),
      ),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    ]
  }

  it('labels an inherited license as coming from the group, not the platform default', async () => {
    server.use(...inheritedHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    // The resolved value is the group's license, and the hint attributes it to the group.
    await waitFor(() => expect(screen.getByText('(inherited from group)')).toBeInTheDocument())
    expect(screen.getByText('CC0-1.0')).toBeInTheDocument()
    expect(screen.queryByText('(platform default)')).toBeNull()
  })
})

describe('EvaluationDetailPage — metrics dashboard', () => {
  const metricsSample: EvaluationMetricsResponse = {
    scope: 'full',
    evaluation_id: EVAL_ID,
    title: 'Test evaluation',
    models_assigned: 2,
    submissions: { total: 4, pending: 1, approved: 2, rejected: 1 },
    // 3 successful-exploit *reviews* but only 2 distinct exploited *submissions* (a flag confirmed by
    // two reviewers) — the rate uses the backend distinct count (2/4 = 50%), never the review count
    // (3/4 = 75%, which the old formula produced and could even exceed 100%).
    reviews: {
      total: 3,
      completed: 3,
      pending: 0,
      successful_exploit: 3,
      unique_exploit: 1,
      valid_submission: 2,
    },
    activity: { conversations: 5, messages: 40 },
    // 2 of the 40 messages reported usage — the averages divide by that, not by 40.
    tokens: {
      prompt_tokens: 900,
      completion_tokens: 300,
      total_tokens: 1200,
      messages_with_usage: 2,
      conversations_with_usage: 1,
      avg_tokens_per_message: 600,
      avg_tokens_per_conversation: 1200,
    },
    exploited_submissions: 2,
    scenarios: [
      {
        scenario_id: 's1',
        name: 'System-prompt leak',
        submissions_total: 2,
        tasks: [],
        tokens: {
          prompt_tokens: 680,
          completion_tokens: 195,
          total_tokens: 875,
          messages_with_usage: 2,
          conversations_with_usage: 1,
          avg_tokens_per_message: 437.5,
          avg_tokens_per_conversation: 875,
        },
      },
    ],
    // Dense on this surface: the standalone dashboard has no group series to borrow an axis from.
    // Sums to submissions.total (4) and to exploited_submissions (2).
    submissions_by_day: [
      { day: '2026-08-03', submissions: 3, exploited_submissions: 2 },
      { day: '2026-08-04', submissions: 0, exploited_submissions: 0 },
      { day: '2026-08-05', submissions: 1, exploited_submissions: 0 },
    ],
    exploits_by_prompt_count: [
      { prompt_count: 1, exploit_count: 1, avg_tokens_to_exploit: 120, exploits_with_tokens: 1 },
      // Second bucket reports no usage — the label must drop the cost rather than print "0 tokens".
      { prompt_count: 3, exploit_count: 1, avg_tokens_to_exploit: null, exploits_with_tokens: 0 },
      // Reported usage that summed to zero — still "not reported", so the cost drops here too.
      { prompt_count: 5, exploit_count: 1, avg_tokens_to_exploit: 0, exploits_with_tokens: 1 },
    ],
    exploits_by_model: [
      // Masking is one flag over one roster, so both distributions show the same label per
      // assignment: `a1` has a `model_display_mask` set, `a2` does not.
      { evaluation_ai_model_id: 'a1', model_alias: 'Model A', exploit_count: 2 },
      { evaluation_ai_model_id: 'a2', model_alias: null, exploit_count: 0 },
    ],
    tokens_by_model: [
      {
        evaluation_ai_model_id: 'a1',
        model_alias: 'Model A',
        tokens: {
          prompt_tokens: 900,
          completion_tokens: 300,
          total_tokens: 1200,
          messages_with_usage: 2,
          conversations_with_usage: 1,
          avg_tokens_per_message: 600,
          avg_tokens_per_conversation: 1200,
        },
      },
      // Assigned but never used — the zero-inclusive row the backend always emits.
      {
        evaluation_ai_model_id: 'a2',
        model_alias: null,
        tokens: {
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
          messages_with_usage: 0,
          conversations_with_usage: 0,
          avg_tokens_per_message: 0,
          avg_tokens_per_conversation: 0,
        },
      },
    ],
  }

  // Group detail carrying the in-group view-metrics authority the page gates the fetch on.
  function groupWithViewMetrics(userPermissions: string[]) {
    return http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
      HttpResponse.json({
        id: GROUP_ID,
        title: 'Spring jailbreak',
        description: '',
        status: 'draft',
        access_level: 'invitation_only',
        start_date: '2026-01-01',
        end_date: null,
        created_by_id: 'user-0001',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        evaluations: [],
        user_permissions: userPermissions,
      }),
    )
  }

  it('renders the metric cards + exploit distributions for a view-metrics owner', async () => {
    // Overrides first — MSW uses the first matching handler, so these must precede baseHandlers'
    // generic group handler (which carries no user_permissions).
    server.use(
      groupWithViewMetrics(['evaluation_groups:view_metrics']),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/metrics`, () =>
        HttpResponse.json(metricsSample),
      ),
      ...baseHandlers(),
    )
    const user = userEvent.setup()
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    // Overview carries no metrics any more — they live behind the Metrics tab.
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Metrics' })).toBeInTheDocument())
    expect(screen.queryByText('Reviews & exploits')).toBeNull()
    await user.click(screen.getByRole('tab', { name: 'Metrics' }))

    // Reused group roll-up card + derived exploit rate over distinct exploited submissions
    // (2 distinct flags / 4 submissions = 50%), NOT the 3 successful-exploit reviews (75%).
    await waitFor(() => expect(screen.getByText('Reviews & exploits')).toBeInTheDocument())
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.queryByText('75%')).toBeNull()
    // Both distributions render, incl. a zero-exploit model (zero-inclusive).
    expect(screen.getByText('Exploits by prompt count')).toBeInTheDocument()
    // The bucket label now carries what one exploit cost to reach beside the prompt count…
    expect(screen.getByText('1 prompt · ~120 tokens')).toBeInTheDocument()
    // …and drops the cost entirely for the bucket whose provider reported nothing, since "0 tokens"
    // would read as a free exploit rather than as an unmeasured one.
    expect(screen.getByText('3 prompts')).toBeInTheDocument()
    expect(screen.getByText('5 prompts')).toBeInTheDocument()
    expect(screen.queryByText(/~0 tokens/)).toBeNull()
    expect(screen.getByText('Exploits by model')).toBeInTheDocument()
    // Each assignment labels a row in both distributions, so each label is expected twice.
    expect(screen.getAllByText('Model A')).toHaveLength(2)
    expect(screen.getByText('Tokens')).toBeInTheDocument()
    expect(screen.getByText('Tokens by model')).toBeInTheDocument()
    // The card's own numbers are covered in metrics-section.test.tsx; here we only prove the
    // page wires the data through — the coverage footnote is enough to show it is the real object.
    expect(
      screen.getByText(/Measured on 2 replies across 1 conversation that reported usage/),
    ).toBeInTheDocument()
    // The `?? 'Masked model'` fallback, in both by-model cards.
    expect(screen.getAllByText('Masked model')).toHaveLength(2)
    // Per-scenario spend: the response carries it and this page renders it, so "where did the
    // tokens go" is answerable for a single evaluation.
    expect(screen.getByText('Tokens by scenario')).toBeInTheDocument()
    expect(screen.getByText('System-prompt leak')).toBeInTheDocument()
    expect(screen.getByText('875')).toBeInTheDocument()
  })

  it('hides the metrics section (and never fetches it) without view-metrics authority', async () => {
    let metricsCalls = 0
    server.use(
      groupWithViewMetrics(['evaluation_groups:read']),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/metrics`, () => {
        metricsCalls += 1
        return HttpResponse.json(metricsSample)
      }),
      ...baseHandlers(),
    )
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByText('Exploits by prompt count')).toBeNull()
    expect(screen.queryByText('Exploits by model')).toBeNull()
    expect(metricsCalls).toBe(0)
  })

  it('shows no tab bar for a viewer holding neither authority', async () => {
    server.use(groupWithViewMetrics(['evaluation_groups:read']), ...baseHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('tablist')).toBeNull()
    // …and no panel left pointing at a tab that was never rendered: an orphan `tabpanel` has no
    // accessible name and wraps the whole body in a tab stop.
    expect(screen.queryByRole('tabpanel')).toBeNull()
    expect(document.getElementById('tab-overview')).toBeNull()
  })
})

describe('EvaluationDetailPage — tagging opt-out', () => {
  it('renders the Allowed tags card by default', async () => {
    server.use(...baseHandlers())
    renderPage(['evaluations:read', 'evaluations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.getByText(/allowed tags/i)).toBeInTheDocument()
  })

  it('hides the card (and never fetches the keys) when tagging is disabled', async () => {
    let tagKeyCalls = 0
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/tag-keys`, () => {
        tagKeyCalls += 1
        return HttpResponse.json([])
      }),
      ...baseHandlers({ ...baseEvaluation, tags_enabled: false }),
    )
    renderPage(['evaluations:read', 'evaluations:update'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByText(/allowed tags/i)).toBeNull()
    expect(tagKeyCalls).toBe(0)
  })
})

describe('EvaluationDetailPage — in-group authority', () => {
  // The group fetch is what carries object-scope authority, so these override the generic
  // handler in baseHandlers() (which returns no user_permissions) and must precede it.
  function groupWith(userPermissions: string[]) {
    return http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
      HttpResponse.json({
        id: GROUP_ID,
        title: 'Spring jailbreak',
        description: '',
        status: 'approved',
        access_level: 'invitation_only',
        start_date: '2026-01-01',
        end_date: null,
        created_by_id: 'user-0001',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        evaluations: [],
        user_permissions: userPermissions,
      }),
    )
  }

  it('offers the Exports tab on in-group evaluation_groups:export', async () => {
    server.use(groupWith(['evaluation_groups:export']), ...baseHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Exports' })).toBeInTheDocument())
  })

  it('does not offer the Exports tab on in-group evaluation_groups:update alone', async () => {
    // Export authority is deliberately its own permission: editing a group must not confer the
    // bundle of every member's transcripts, flags and reviews.
    server.use(groupWith(['evaluation_groups:update']), ...baseHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('tab', { name: 'Exports' })).toBeNull()
  })

  it('offers New conversation when only the in-group role grants conversations:create', async () => {
    server.use(groupWith(['conversations:read', 'conversations:create']), ...baseHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /new conversation/i })).toBeInTheDocument(),
    )
  })

  it('offers the header Start a conversation on the same in-group authority', async () => {
    // Three entry points lead to the same create flow (header CTA, section button, scenario
    // Start); one of them disagreeing on the source of authority is how the last bug survived.
    server.use(groupWith(['conversations:read', 'conversations:create']), ...baseHandlers())
    renderPage(['evaluations:read', 'evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test evaluation' })).toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /start a conversation/i })).toBeInTheDocument(),
    )
  })
})
