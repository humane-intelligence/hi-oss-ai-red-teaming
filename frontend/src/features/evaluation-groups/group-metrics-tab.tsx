import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { SubmissionsTimelineChart } from '@/components/shared/submissions-timeline-chart'
import { SubmissionsSparkline } from '@/components/shared/submissions-sparkline'
import { humanizeError } from '@/lib/api/problem'
import {
  ActivityCard,
  EvaluationMetricsDetail,
  GroupTokensByModelCard,
  MetricsScopeBadge,
  ReviewsCard,
  SubmissionsCard,
  TokensCard,
} from './metrics-section'
import type { EvaluationGroupMetricsResponse } from '@/lib/api/types'

// The group's whole metrics surface, in one place. Overview deliberately carries none of it: the
// same numbers on two tabs is the drift surface this move exists to remove.
export function GroupMetricsTab({
  data,
  isError,
  error,
}: {
  data: EvaluationGroupMetricsResponse | undefined
  isError: boolean
  error: unknown
}) {
  // Error copy only when there is nothing to show: a failed *background* refetch keeps the
  // last-good numbers rather than stacking a destructive card over live data.
  if (!data) {
    if (!isError) return <p className="text-muted-foreground text-sm">Loading metrics…</p>
    return (
      <Card>
        <CardHeader>
          <CardTitle>Metrics</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-destructive text-sm">{humanizeError(error)}</p>
        </CardContent>
      </Card>
    )
  }

  // Hoisted out of the evaluations map below: one array for every sparkline, not one per card.
  const axis = data.submissions_by_day.map((point) => point.day)

  return (
    <div className="space-y-6">
      <MetricsScopeBadge scope={data.scope} />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2">
        <SubmissionsCard submissions={data.submissions} />
        <ReviewsCard reviews={data.reviews} />
        <ActivityCard activity={data.activity} submissions={data.submissions} />
        <TokensCard tokens={data.tokens} />
      </div>
      <GroupTokensByModelCard
        tokensByModel={data.tokens_by_model}
        withheld={data.tokens_by_model_withheld}
      />
      <Card>
        <CardHeader>
          <CardTitle>Submissions over time</CardTitle>
        </CardHeader>
        <CardContent>
          <SubmissionsTimelineChart points={data.submissions_by_day} />
        </CardContent>
      </Card>
      {data.evaluations.length > 0 && (
        <div className="space-y-4">
          <h2 className="text-sm font-medium">Per evaluation</h2>
          {data.evaluations.map((evaluation) => (
            <Card key={evaluation.evaluation_id}>
              <CardHeader>
                <CardTitle>{evaluation.title}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {/* On the group's own dense axis, so the gaps are real and two evaluations are
                    comparable — the per-evaluation array is sparse by contract. */}
                <SubmissionsSparkline sparse={evaluation.submissions_by_active_day} axis={axis} />
                <EvaluationMetricsDetail evaluation={evaluation} />
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
