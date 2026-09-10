import { UserPlus } from 'lucide-react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useReviewQueue } from './queries'
import { VerdictDialog } from './verdict-dialog'
import { AssignReviewerDialog, type FlagRef } from './assign-reviewer-dialog'
import { verdictSummary } from './format'
import { useUserLookup } from '@/features/users/queries'
import { useEvaluationLookup } from '@/features/evaluations/queries'
import { usePermissions } from '@/lib/auth/use-permissions'
import { Pagination } from '@/components/shared/pagination'
import { DataTable, type Column } from '@/components/shared/data-table'
import { ReviewProgress } from '@/components/shared/review-progress'
import { StatusPill } from '@/components/shared/status-pill'
import { Button } from '@/components/ui/button'
import type { ReviewQueueItem, ReviewResponse } from '@/lib/api/types'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

const PAGE_SIZE = 20

export function ReviewQueuePage() {
  // Both the filter and the page live in the URL. The shortcut tile sits in the layout above this
  // route's `Outlet`, so arriving from it changes search params without remounting — an `offset`
  // kept in component state would survive that and answer page 2 of a list with four rows.
  const [params, setParams] = useSearchParams()
  // Read the value, not just the key: the tile and the checkbox both write `=1`, so a bare
  // `has()` would turn the filter on for the `?unassigned=0` a reader can type into the
  // linkable queue URL. `true` is accepted too, since that is what the API itself takes.
  const unassignedParam = params.get('unassigned')?.toLowerCase()
  const unassigned = unassignedParam === '1' || unassignedParam === 'true'
  // Clamped, for the same reason: negatives and junk floor to 0, fractions truncate, and the
  // ceiling keeps `String` out of exponential notation — each would otherwise be a 422 toast.
  const offset = Math.min(
    Number.MAX_SAFE_INTEGER,
    Math.trunc(Math.max(0, Number(params.get('offset') ?? '0') || 0)),
  )
  const setOffset = (next: number) =>
    setParams((prev) => {
      const url = new URLSearchParams(prev)
      if (next > 0) url.set('offset', String(next))
      else url.delete('offset')
      return url
    })
  const [verdictReview, setVerdictReview] = useState<ReviewResponse | null>(null)
  const [assignFlags, setAssignFlags] = useState<FlagRef[] | null>(null)
  const query = useReviewQueue({ limit: PAGE_SIZE, offset, unassigned })
  const lookup = useUserLookup()
  const evaluationName = useEvaluationLookup()
  const { has } = usePermissions()
  const page = query.data
  const canAssign = has('reviews:create')

  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set())

  // Selection is page-scoped: prune keys not in the current rows during render (paging/refetch drop),
  // which also clears it on a page change since the new page's ids differ. Not an effect (eslint bans
  // setState-in-effect); the prev-signature guard makes it converge.
  const rowIds = new Set((page?.items ?? []).map((i) => i.submission.id))
  const rowSig = [...rowIds].join(' ')
  const [seenSig, setSeenSig] = useState(rowSig)
  if (rowSig !== seenSig) {
    setSeenSig(rowSig)
    setSelectedKeys((prev) => {
      if (prev.size === 0) return prev
      const next = new Set([...prev].filter((k) => rowIds.has(k)))
      return next.size === prev.size ? prev : next
    })
  }

  const selectedFlags: FlagRef[] = (page?.items ?? [])
    .filter((i) => selectedKeys.has(i.submission.id))
    .map((i) => ({ id: i.submission.id, label: i.submission.reason }))

  // One conversation can carry many independent flags; note how many share a
  // conversation so the reviewer reads them as related, not unrelated rows.
  const convCounts = new Map<string, number>()
  for (const i of page?.items ?? []) {
    const id = i.submission.conversation_id
    convCounts.set(id, (convCounts.get(id) ?? 0) + 1)
  }

  // Reviews-so-far live in the expander (was the card body): status + reviewer + verdict/edit action.
  // Kept collapsed on purpose — a flag can carry many reviewers (100+), and an always-on list would be
  // an unbounded ribbon per row; the progress count stays visible, the roster is one click away.
  const reviewsSoFar = (item: ReviewQueueItem) => (
    <div className="space-y-2">
      {item.reviews.map((r) => (
        <div
          key={r.id}
          className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm"
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <StatusPill status={r.status} />
              <span className="text-muted-foreground truncate">
                {r.reviewer_email ?? lookup(r.reviewer_id)}
              </span>
            </div>
            {r.status !== 'pending' && (
              <div className="text-muted-foreground mt-1 text-xs">{verdictSummary(r)}</div>
            )}
          </div>
          {has('reviews:update') && (
            <Button size="sm" variant="outline" onClick={() => setVerdictReview(r)}>
              {r.status === 'pending' ? 'Record verdict' : 'Edit verdict'}
            </Button>
          )}
        </div>
      ))}
      {item.reviews.length === 0 && (
        <p className="text-muted-foreground text-sm">No reviewers assigned yet.</p>
      )}
    </div>
  )

  const columns: Column<ReviewQueueItem>[] = [
    {
      header: 'Reason',
      cell: (item) => (
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={`/reviews/submissions/${item.submission.id}`}
            className="font-medium underline-offset-2 hover:underline"
          >
            {item.submission.reason}
          </Link>
          {item.submission.red_flagged && <StatusPill status="red_flagged" />}
        </div>
      ),
    },
    {
      header: 'Evaluation',
      hideBelow: 'lg',
      cell: (item) => (
        <span className="text-muted-foreground text-sm">
          {evaluationName(item.submission.evaluation_id)}
        </span>
      ),
    },
    {
      header: 'Flagged',
      hideBelow: 'lg',
      cell: (item) => {
        const convCount = convCounts.get(item.submission.conversation_id) ?? 1
        return (
          <div className="text-muted-foreground flex flex-col gap-0.5 text-xs">
            <span>flagged {new Date(item.submission.created_at).toLocaleDateString()}</span>
            <Link
              to={`/evaluations/${item.submission.evaluation_id}/conversations/${item.submission.conversation_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-foreground underline-offset-2 hover:underline"
            >
              View conversation ({item.submission.messages.length} msg) ↗
            </Link>
            {convCount > 1 && (
              <span className="text-warn">{convCount} flags from this conversation</span>
            )}
          </div>
        )
      },
    },
    {
      header: 'Progress',
      hideBelow: 'sm',
      cell: (item) => (
        <div className="flex flex-col items-start gap-0.5">
          <ReviewProgress completed={item.completed_reviews} required={item.required_reviews} />
          {item.submission.scenario_id == null && (
            <span className="text-muted-foreground/70 font-mono text-[9px] tracking-wide uppercase">
              no challenge · default 1
            </span>
          )}
        </div>
      ),
    },
    {
      header: '',
      className: 'text-right',
      cell: (item) =>
        canAssign ? (
          <Button
            size="sm"
            variant="outline"
            // Named by its row: below `lg` the label is hidden, so this is all that separates one
            // button from the next, as `users-list-page` does with the email.
            aria-label={`Assign reviewer to: ${item.submission.reason}`}
            title="Assign reviewer"
            onClick={() =>
              setAssignFlags([{ id: item.submission.id, label: item.submission.reason }])
            }
          >
            <UserPlus />
            {/* The label is the widest thing in the row; below `lg` the icon carries it. */}
            <span className="hidden lg:inline">Assign reviewer</span>
          </Button>
        ) : null,
    },
  ]

  return (
    <div className="space-y-4">
      <Label className="text-muted-foreground w-fit text-sm font-normal">
        <Checkbox
          checked={unassigned}
          onCheckedChange={(next) => {
            setParams(
              (prev) => {
                const p = new URLSearchParams(prev)
                if (next === true) p.set('unassigned', '1')
                else p.delete('unassigned')
                // Page 2 of the whole queue is not page 2 of the narrowed one.
                p.delete('offset')
                return p
              },
              // A filter toggle is not a navigation: Back should leave the queue, not walk the
              // states the reader flipped through.
              { replace: true },
            )
          }}
        />
        Unassigned only
      </Label>
      {selectedKeys.size > 0 && (
        <div
          aria-live="polite"
          className="bg-muted/40 flex items-center gap-3 rounded-md border px-3 py-2 text-sm"
        >
          <span className="font-medium">
            {selectedKeys.size} flag{selectedKeys.size === 1 ? '' : 's'} selected
          </span>
          <Button size="sm" onClick={() => setAssignFlags(selectedFlags)}>
            Assign reviewers
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSelectedKeys(new Set())}>
            Clear
          </Button>
        </div>
      )}
      <DataTable
        columns={columns}
        rows={page?.items}
        rowKey={(item) => item.submission.id}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        emptyLabel={unassigned ? 'No unassigned flags.' : 'Nothing awaiting review.'}
        emptyHint={
          unassigned
            ? 'Clear the filter to see flags that are already being reviewed.'
            : 'Flagged messages assigned to reviewers show up here.'
        }
        renderExpanded={reviewsSoFar}
        selection={canAssign ? { selectedKeys, onSelectedChange: setSelectedKeys } : undefined}
      />

      {page && (
        <Pagination
          offset={offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={setOffset}
        />
      )}

      <VerdictDialog review={verdictReview} onClose={() => setVerdictReview(null)} />
      <AssignReviewerDialog
        flags={assignFlags ?? []}
        open={assignFlags !== null}
        onOpenChange={(o) => {
          if (!o) {
            setAssignFlags(null)
            setSelectedKeys(new Set())
          }
        }}
      />
    </div>
  )
}
