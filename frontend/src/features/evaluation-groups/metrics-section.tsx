import type { ReactNode } from 'react'
import { BarList } from '@/components/shared/bar-list'
import { DataTable, type Column } from '@/components/shared/data-table'
import { InfoHint } from '@/components/shared/info-hint'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import type {
  EvaluationGroupMetricsResponse,
  EvaluationMetrics,
  MetricsScope,
  ScenarioMetrics,
  TokenMetrics,
} from '@/lib/api/types'

type Metrics = EvaluationGroupMetricsResponse

// Shown above either dashboard when the read is filtered to the caller's own data
// (a `members_personal_metrics` group seen by a `view_personal_metrics` member).
// Renders nothing for the full event-wide aggregate.
export function MetricsScopeBadge({ scope }: { scope: MetricsScope }) {
  if (scope !== 'personal') return null
  return <Badge variant="secondary">Personal view — showing only your own data</Badge>
}

// A label + value pair for the breakdown cards — muted label, tabular value.
// The label yields space first: in the four-column grid a token count reaches nine digits, which
// overflows the cell unless the label may shrink below its text width.
function CountRow({ label, value, strong }: { label: string; value: ReactNode; strong?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <span className="text-muted-foreground min-w-0 truncate text-sm">{label}</span>
      <span className={`tabular-nums ${strong ? 'font-semibold' : 'font-medium'}`}>{value}</span>
    </div>
  )
}

export function SubmissionsCard({ submissions }: { submissions: Metrics['submissions'] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Submissions</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <CountRow label="Total" value={submissions.total} strong />
        <CountRow label="Pending" value={submissions.pending} />
        <CountRow label="Approved" value={submissions.approved} />
        <CountRow label="Rejected" value={submissions.rejected} />
      </CardContent>
    </Card>
  )
}

export function ReviewsCard({ reviews }: { reviews: Metrics['reviews'] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Reviews &amp; exploits</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <CountRow label="Total" value={reviews.total} strong />
        <CountRow label="Completed" value={reviews.completed} />
        <CountRow label="Pending" value={reviews.pending} />
        <CountRow label="Successful exploit" value={reviews.successful_exploit} />
        <CountRow label="Unique exploit" value={reviews.unique_exploit} />
        <CountRow label="Valid submission" value={reviews.valid_submission} />
      </CardContent>
    </Card>
  )
}

// The two ratios are derived here rather than served: `ActivityMetrics` carries only the two counts,
// and these are the readings an organiser actually asks for — how deep conversations run, and how
// often one ends in a submission. Anything dated (activity per day, unique active members) needs the
// backend to date it first.
//
// An empty denominator reads as unmeasured, never as `0`: "0 messages per conversation" would claim
// a measurement that nobody made.
function ratio(numerator: number, denominator: number): string {
  if (denominator === 0) return '—'
  const value = numerator / denominator
  // One decimal below 10 so a real 0.8 is not rounded away to 1; whole numbers above it.
  return value < 10 ? String(Math.round(value * 10) / 10) : String(Math.round(value))
}

export function ActivityCard({
  activity,
  submissions,
}: {
  activity: Metrics['activity']
  submissions: Metrics['submissions']
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <CountRow label="Conversations" value={activity.conversations} />
        <CountRow label="Messages" value={activity.messages} />
        {/* `conv` rather than "per conversation": measured at a 768 px viewport the card is ~190 px
            wide in the two-column grid and the long labels hit `CountRow`'s truncate. The repo
            already abbreviates it this way in the per-evaluation breakdown ("2 conv"). */}
        <CountRow
          label="Messages / conv"
          value={ratio(activity.messages, activity.conversations)}
        />
        <CountRow
          label="Submissions / conv"
          value={ratio(submissions.total, activity.conversations)}
        />
      </CardContent>
    </Card>
  )
}

// Token counts reach tens of thousands, where a bare digit run stops being readable.
// Deliberately not exported: this file exports components, and Fast Refresh only works when a
// module exports components alone (react-refresh/only-export-components).
function formatTokens(value: number): string {
  return value.toLocaleString()
}

// Faithful integer percent: never round a real share down to "0%", so a small but non-zero
// part reads as small rather than as absent.
function formatShare(part: number, total: number): string {
  if (total === 0) return '—'
  const rounded = Math.round((part / total) * 100)
  if (part > 0 && rounded === 0) return '<1%'
  return `${rounded}%`
}

// Scope-neutral: this card also renders per evaluation and under a `personal` filter, so naming
// any one scope is false in the others.
const TOTAL_HINT =
  'Every token the providers reported for the conversations counted here: the prompts sent to the models plus the replies they generated.'
const INPUT_HINT =
  'Tokens in what was sent to the model. Each turn resends the whole conversation so far, so later turns re-count earlier ones — this is what providers bill for, not the length of the last prompt alone.'
const OUTPUT_HINT = 'Tokens in the text the models generated.'

// One label + amount + share row of the split.
function SplitRow({
  label,
  hint,
  value,
  total,
}: {
  label: string
  hint: string
  value: number
  total: number
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-sm">
      <span className="text-muted-foreground flex min-w-0 items-center gap-1.5">
        <span className="truncate">{label}</span>
        <InfoHint text={hint} />
      </span>
      <span className="shrink-0 tabular-nums">
        <span className="font-medium">{formatTokens(value)}</span>
        <span className="text-muted-foreground"> · {formatShare(value, total)}</span>
      </span>
    </div>
  )
}

// Model token spend. Input and output are *parts of* the total, so they are drawn as one split bar
// instead of three sibling rows — the composition is the point, and summing two numbers to check
// it against a third is work the reader shouldn't have to do.
//
// Two shapes the layout has to stay honest about:
//   - A provider may report a total with no breakdown, leaving input and output at zero. The bar
//     is dropped then rather than drawn empty next to a non-zero total.
//   - The averages divide by what actually reported usage, not by every message — a provider
//     reports usage on completed replies only, so an all-messages denominator would understate
//     the cost. The footnote names both denominators so the two averages can't be misread.
// When nothing reported usage at all we say so instead of rendering zeros: a model with no working
// credential would otherwise read as free rather than as unmeasured.
export function TokensCard({ tokens }: { tokens: TokenMetrics }) {
  const {
    prompt_tokens,
    completion_tokens,
    total_tokens,
    messages_with_usage,
    conversations_with_usage,
  } = tokens
  const hasSplit = prompt_tokens + completion_tokens > 0
  // Clamped: a provider reporting a breakdown that exceeds its own total can't overflow the track.
  const segment = (part: number) =>
    total_tokens > 0 ? `${Math.min(100, (part / total_tokens) * 100)}%` : '0%'
  return (
    <Card>
      <CardHeader>
        <CardTitle>Tokens</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {messages_with_usage === 0 || total_tokens === 0 ? (
          <p className="text-muted-foreground text-sm">
            Providers reported no tokens for these replies.
          </p>
        ) : (
          <>
            <div className="flex items-baseline gap-2">
              <span className="text-2xl leading-none font-semibold tabular-nums">
                {formatTokens(total_tokens)}
              </span>
              <span className="text-muted-foreground flex items-center gap-1.5 text-sm">
                tokens total
                <InfoHint text={TOTAL_HINT} />
              </span>
            </div>
            {hasSplit ? (
              <div className="space-y-1.5">
                {/* One fill, not two segments: the bar carries the input share and the rows below
                    carry both amounts with their percentages. */}
                <div className="bg-muted h-2 overflow-hidden rounded-full">
                  <div className="bg-primary h-2" style={{ width: segment(prompt_tokens) }} />
                </div>
                <SplitRow
                  label="Input (prompts)"
                  hint={INPUT_HINT}
                  value={prompt_tokens}
                  total={total_tokens}
                />
                <SplitRow
                  label="Output (replies)"
                  hint={OUTPUT_HINT}
                  value={completion_tokens}
                  total={total_tokens}
                />
              </div>
            ) : (
              <p className="text-muted-foreground text-xs">
                The providers reported a total without an input/output breakdown.
              </p>
            )}
            <div className="space-y-1 border-t pt-2.5">
              <CountRow
                label="Average per reply"
                value={formatTokens(Math.round(tokens.avg_tokens_per_message))}
              />
              <CountRow
                label="Average per conversation"
                value={formatTokens(Math.round(tokens.avg_tokens_per_conversation))}
              />
            </div>
            <p className="text-muted-foreground text-xs">
              Measured on {messages_with_usage} {messages_with_usage === 1 ? 'reply' : 'replies'}{' '}
              across {conversations_with_usage}{' '}
              {conversations_with_usage === 1 ? 'conversation' : 'conversations'} that reported
              usage.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

const GROUP_TOKENS_BY_MODEL_HINT =
  'What each model cost across the conversations counted here — one row per model, summing every evaluation it was assigned to. A model whose every assignment was since removed keeps its spend in the total above but appears in no row here, so these can sum to less than that total.'

// Group-wide spend per model. The backend serves this only to a full-access reader, or when no
// evaluation in the group masks model names — a row correlates one model's cost across evaluations,
// which per-evaluation masking exists to withhold.
//
// An empty list has two meanings, so the backend states which one via `tokens_by_model_withheld`
// rather than leaving each client to guess. Saying "no models assigned" to a member of a six-model
// group would be a lie. The other branch leans on the list being zero-inclusive over live
// assignments (`GroupTokensByModelMetrics`) — a spend-only list would make it claim "no models
// assigned" for every group that has not run yet.
export function GroupTokensByModelCard({
  tokensByModel,
  withheld,
}: {
  tokensByModel: Metrics['tokens_by_model']
  withheld: Metrics['tokens_by_model_withheld']
}) {
  return (
    <BarList
      title="Tokens by model"
      hint={GROUP_TOKENS_BY_MODEL_HINT}
      emptyLabel={
        withheld
          ? 'Per-model spend is hidden because an evaluation in this group masks model names. The totals above still cover every model.'
          : 'No models assigned yet.'
      }
      rows={tokensByModel.map((model) => ({
        key: model.ai_model_id,
        label: model.model_alias,
        value: model.tokens.total_tokens,
      }))}
    />
  )
}

// No column here carries `hideBelow`: the table sits in a card with no expander and no detail
// route, so a dropped number is gone rather than one tap away. The headers are what would not fit
// ("Submissions" needs 85px against the 58px its column gets), so they abbreviate instead.
const scenarioColumns: Column<ScenarioMetrics>[] = [
  { header: 'Scenario', cell: (s) => <span className="font-medium">{s.name}</span> },
  {
    header: (
      <>
        <span className="sm:hidden">Subs</span>
        <span className="hidden sm:inline">Submissions</span>
      </>
    ),
    cell: (s) => <span className="tabular-nums">{s.submissions_total}</span>,
  },
  {
    header: (
      <>
        <span className="sm:hidden">Tok</span>
        <span className="hidden sm:inline">Tokens</span>
      </>
    ),
    cell: (s) => <span className="tabular-nums">{formatTokens(s.tokens.total_tokens)}</span>,
  },
  {
    header: 'Tasks',
    cell: (s) =>
      s.tasks.length === 0 ? (
        <span className="text-muted-foreground">—</span>
      ) : (
        <div className="flex flex-wrap gap-1">
          {/* `whitespace-nowrap` from `sm`: this table sits in the Metrics tab's card, which is
              narrow enough that without it a chip breaks between the task name and its count -
              "Demo Task" on one line, "· 0" on the next. Chips wrap as whole chips (the flex-wrap
              above), never mid-label. Below `sm` the column has no width to spare, so the chip is
              allowed to break after all. */}
          {s.tasks.map((t) => (
            <Badge key={t.task_id} variant="outline" className="sm:whitespace-nowrap">
              {t.name} · <span className="tabular-nums">{t.submissions_total}</span>
            </Badge>
          ))}
        </div>
      ),
  },
]

// The per-evaluation breakdown shown inside an expanded Evaluations row: submission/
// review/activity roll-ups plus the scenario→task submission counts. No Card wrapper —
// it sits in a table cell whose row already carries the evaluation's title/status.
export function EvaluationMetricsDetail({ evaluation }: { evaluation: EvaluationMetrics }) {
  const { submissions, reviews, activity, tokens } = evaluation
  return (
    <div className="space-y-4">
      {/* Two columns before four: this breakdown used to live in a full-width expanded table row,
          but in the Metrics tab it sits in a card, and `sm:` keys off the viewport rather than the
          container — measured at a 768 px viewport four columns left ~95 px each and `CountRow`'s
          truncate ate the labels ("Submi…", "Acti…"). */}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <div className="space-y-1">
          <CountRow label="Submissions" value={submissions.total} />
          <p className="text-muted-foreground text-xs tabular-nums">
            {submissions.pending} pending · {submissions.approved} approved · {submissions.rejected}{' '}
            rejected
          </p>
        </div>
        <div className="space-y-1">
          <CountRow label="Reviews" value={reviews.total} />
          <p className="text-muted-foreground text-xs tabular-nums">
            {reviews.completed} done · {reviews.pending} pending · {reviews.successful_exploit}{' '}
            exploit
          </p>
        </div>
        <div className="space-y-1">
          <CountRow label="Activity" value={`${activity.conversations} conv`} />
          <p className="text-muted-foreground text-xs tabular-nums">{activity.messages} messages</p>
        </div>
        <div className="space-y-1">
          <CountRow label="Tokens" value={formatTokens(tokens.total_tokens)} />
          <p className="text-muted-foreground text-xs tabular-nums">
            {tokens.messages_with_usage === 0 || tokens.total_tokens === 0
              ? 'none reported'
              : `avg ${formatTokens(Math.round(tokens.avg_tokens_per_message))} per reply`}
          </p>
        </div>
      </div>
      {/* Bleeds into the card's own padding below `sm`: this is the narrowest table in the app and
          the four columns want those last 16px. */}
      <div className="-mx-2 sm:mx-0">
        <DataTable
          columns={scenarioColumns}
          rows={evaluation.scenarios}
          rowKey={(s) => s.scenario_id}
          emptyLabel="No scenarios in this evaluation."
        />
      </div>
    </div>
  )
}
