import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useReviews } from './queries'
import { verdictSummary } from './format'
import { useEvaluationLookup } from '@/features/evaluations/queries'
import { useAuth } from '@/lib/auth/auth-context'
import { DataTable, type Column } from '@/components/shared/data-table'
import { Pagination } from '@/components/shared/pagination'
import { StatusPill } from '@/components/shared/status-pill'
import type { ReviewResponse } from '@/lib/api/types'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

const PAGE_SIZE = 20

// Reviewer worklist: reviews assigned to the caller, pending first. The row opens
// the submission detail (flagged messages as context) where the verdict is recorded.
export function MyReviewsPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [pendingOnly, setPendingOnly] = useState(true)
  const evaluationName = useEvaluationLookup()
  const query = useReviews({
    limit: PAGE_SIZE,
    offset,
    status: pendingOnly ? 'pending' : undefined,
    reviewer_id: user?.id,
  })
  const page = query.data

  const columns: Column<ReviewResponse>[] = [
    {
      header: 'Evaluation',
      cell: (r) => <span className="font-medium">{evaluationName(r.evaluation_id)}</span>,
    },
    { header: 'Status', cell: (r) => <StatusPill status={r.status} /> },
    {
      header: 'Verdict',
      hideBelow: 'sm',
      cell: (r) =>
        r.status === 'pending' ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <span className="text-muted-foreground">{verdictSummary(r)}</span>
        ),
    },
    {
      header: 'Created',
      hideBelow: 'md',
      cell: (r) => (
        <span className="text-muted-foreground">{new Date(r.created_at).toLocaleDateString()}</span>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <ToggleGroup
        type="single"
        value={pendingOnly ? 'pending' : 'all'}
        onValueChange={(v) => {
          if (!v) return
          setOffset(0)
          setPendingOnly(v === 'pending')
        }}
        variant="outline"
        size="sm"
      >
        <ToggleGroupItem value="pending">Pending</ToggleGroupItem>
        <ToggleGroupItem value="all">All mine</ToggleGroupItem>
      </ToggleGroup>
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(r) => r.id}
        onRowClick={(r) => navigate(`/reviews/submissions/${r.message_flag_id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        emptyLabel={pendingOnly ? 'Nothing assigned to you right now.' : 'You have no reviews yet.'}
        emptyHint="Review assignments show up here. Pick up work from the Queue tab."
      />
      {page && (
        <Pagination
          offset={offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={setOffset}
        />
      )}
    </div>
  )
}
