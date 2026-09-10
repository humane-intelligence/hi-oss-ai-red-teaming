import { BarList } from '@/components/shared/bar-list'
import { StatTile } from '@/components/shared/stat-tile'
import { SubmissionsTimelineChart } from '@/components/shared/submissions-timeline-chart'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  ActivityCard,
  MetricsScopeBadge,
  ReviewsCard,
  SubmissionsCard,
  TokensCard,
} from '@/features/evaluation-groups/metrics-section'
import type { EvaluationMetricsResponse } from '@/lib/api/types'

const SCENARIO_TOKENS_HINT =
  'Spend of the conversations tagged with each scenario. A conversation started outside any scenario still counts toward the evaluation total but appears in no row here, so these can sum to less than the total above.'

const MODEL_EXPLOITS_HINT =
  'Successful exploits per model assigned to this evaluation. An exploit against an assignment that was since removed still counts in the reviews total but appears in no row here.'

const MODEL_TOKENS_HINT =
  'Spend per model assigned to this evaluation. Spend against an assignment that was since removed stays in the total above but appears in no row here, so these can sum to less than that total.'

const EXPLOIT_COST_HINT =
  'The number on each row is how many exploits landed at that prompt count. The tokens beside the label are what one of them cost on average to reach — counting whole turns up to and including the one that broke the model, averaged over the exploits whose provider reported usage.'

// "3 prompts · ~438 tokens": the bar already carries how many exploits, so the label carries what
// one cost. Dropped whenever it would round to zero, null included: "~0 tokens" reads as "free"
// rather than as "not reported".
function formatBucketLabel(
  bucket: EvaluationMetricsResponse['exploits_by_prompt_count'][number],
): string {
  const prompts = `${bucket.prompt_count} prompt${bucket.prompt_count === 1 ? '' : 's'}`
  if (bucket.exploits_with_tokens === 0 || bucket.avg_tokens_to_exploit === null) return prompts
  const cost = Math.round(bucket.avg_tokens_to_exploit)
  if (cost === 0) return prompts
  return `${prompts} · ~${cost.toLocaleString()} tokens`
}

// Faithful integer-percent label: never round a real exploit down to "0%" nor an incomplete
// rate up to "100%" — that headline would contradict the distribution charts right below it.
function formatExploitRate(exploited: number, total: number): string {
  if (total === 0) return '—'
  const rounded = Math.round((exploited / total) * 100)
  if (exploited > 0 && rounded === 0) return '<1%'
  if (exploited < total && rounded === 100) return '>99%'
  return `${rounded}%`
}

// Single-evaluation dashboard: the group roll-up cards scoped to this evaluation (reused verbatim
// from the group dashboard) plus a derived exploit rate and the two exploit distributions.
//
// Laid out like the group's Metrics tab rather than like the aside it used to live in — same reading
// order (scope, roll-up, timeline, distributions) and the same two-column grid — so moving between a
// group and one of its evaluations does not re-teach the page.
export function EvaluationMetricsPanel({ metrics }: { metrics: EvaluationMetricsResponse }) {
  const { submissions, reviews, activity } = metrics
  // `exploited_submissions` is the backend's distinct-exploited count and the single source of truth
  // for the rate below. NOT reviews.successful_exploit (per (flag, reviewer), so confirming reviewers
  // could push it past 100%) and NOT a sum of one distribution (by-model omits exploits on
  // soft-deleted assignments, so the two charts could disagree with the headline).
  return (
    <div className="space-y-6">
      <MetricsScopeBadge scope={metrics.scope} />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2">
        <StatTile
          label="Exploit rate"
          value={formatExploitRate(metrics.exploited_submissions, submissions.total)}
        />
        <SubmissionsCard submissions={submissions} />
        <ReviewsCard reviews={reviews} />
        <ActivityCard activity={activity} submissions={submissions} />
        <TokensCard tokens={metrics.tokens} />
      </div>
      {/* Full width, directly after the roll-up: a `personal` read narrows the curve too, and the
          badge above must stay adjacent to it or a personal curve reads as the whole evaluation's. */}
      <Card>
        <CardHeader>
          <CardTitle>Submissions over time</CardTitle>
        </CardHeader>
        <CardContent>
          <SubmissionsTimelineChart points={metrics.submissions_by_day} />
        </CardContent>
      </Card>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2">
        {/* The per-scenario spend the response has always carried but nothing on this page showed:
          the scenario table lives on the group dashboard's Metrics tab, so an operator looking at
          one evaluation could not see where its tokens went. */}
        <BarList
          title="Tokens by scenario"
          hint={SCENARIO_TOKENS_HINT}
          emptyLabel="No scenarios in this evaluation."
          rows={metrics.scenarios.map((scenario) => ({
            key: scenario.scenario_id,
            label: scenario.name,
            value: scenario.tokens.total_tokens,
          }))}
        />
        <BarList
          title="Exploits by prompt count"
          hint={EXPLOIT_COST_HINT}
          emptyLabel="No successful exploits yet."
          rows={metrics.exploits_by_prompt_count.map((bucket) => ({
            key: String(bucket.prompt_count),
            label: formatBucketLabel(bucket),
            value: bucket.exploit_count,
          }))}
        />
        <BarList
          title="Exploits by model"
          hint={MODEL_EXPLOITS_HINT}
          emptyLabel="No models assigned yet."
          rows={metrics.exploits_by_model.map((model) => ({
            key: model.evaluation_ai_model_id,
            // `model_alias` is null for a masked model with no display mask set.
            label: model.model_alias ?? 'Masked model',
            value: model.exploit_count,
          }))}
        />
        <BarList
          title="Tokens by model"
          hint={MODEL_TOKENS_HINT}
          emptyLabel="No models assigned yet."
          rows={metrics.tokens_by_model.map((model) => ({
            key: model.evaluation_ai_model_id,
            label: model.model_alias ?? 'Masked model',
            value: model.tokens.total_tokens,
          }))}
        />
      </div>
    </div>
  )
}
