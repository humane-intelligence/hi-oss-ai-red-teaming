import { useState } from 'react'
import { useUnionAssignableReviewers } from './queries'
import { useBulkAssignReviewers } from './mutations'
import { MultiSearchSelect } from '@/components/shared/multi-search-select'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'

export type FlagRef = { id: string; label: string }

// Assign one or more reviewers to one or more flagged submissions in one action. The picked reviewers ×
// picked flags cartesian is posted via POST /reviews/bulk, pre-filtered to pairs each flag's pool
// accepts; per-pair races (assigned between fetch and POST) surface as per-row failures.
export function AssignReviewerDialog({
  flags,
  open,
  onOpenChange,
}: {
  flags: FlagRef[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [reviewerIds, setReviewerIds] = useState<string[]>([])
  const [search, setSearch] = useState('')
  // (flag, reviewer) pairs already created by a prior partial run this session — excluded from the
  // cartesian below so a retry after a partial failure re-posts only what didn't succeed, not 409s.
  const [doneKeys, setDoneKeys] = useState<Set<string>>(() => new Set())
  const flagIds = flags.map((f) => f.id)
  const pool = useUnionAssignableReviewers(flagIds, { enabled: open, search })
  const bulk = useBulkAssignReviewers()

  const labelByFlag = new Map(flags.map((f) => [f.id, f.label]))
  // `pool.options` is current-search-scoped (it narrows as you type), so accumulate id→email here to keep
  // the failure/skipped summaries readable for a reviewer the search has since narrowed out of the list.
  const [seenEmails, setSeenEmails] = useState<Record<string, string>>({})
  let emailMap = seenEmails
  const pendingEmails: Record<string, string> = {}
  for (const o of pool.options) if (emailMap[o.value] !== o.label) pendingEmails[o.value] = o.label
  if (Object.keys(pendingEmails).length > 0) {
    emailMap = { ...emailMap, ...pendingEmails }
    setSeenEmails(emailMap)
  }
  const emailFor = (id: string) => emailMap[id] ?? id

  // Cartesian, pre-filtered to (flag, reviewer) pairs the flag's pool accepts, minus pairs already
  // created this session (partial-failure retry re-posts only what didn't succeed).
  const rows = flagIds
    .flatMap((flagId) =>
      reviewerIds
        .filter((rid) => pool.poolByFlag.get(flagId)?.has(rid))
        .map((reviewer_id) => ({
          row_key: `${flagId}:${reviewer_id}`,
          data: { message_flag_id: flagId, reviewer_id },
        })),
    )
    .filter((r) => !doneKeys.has(r.row_key))
  // Picked (flag, reviewer) pairs the pre-filter drops because that flag's pool excludes the reviewer
  // (author / already-assigned / not an assignable annotator for that group). Surfaced so the drop isn't
  // silent — a lower total alone doesn't tell the operator *which* selection wasn't applied where. Only
  // once the pools have loaded (an unloaded pool is `undefined`, not "excluded").
  const skipped =
    pool.isPending || pool.isError
      ? []
      : flagIds.flatMap((flagId) =>
          reviewerIds
            .filter((rid) => !pool.poolByFlag.get(flagId)?.has(rid))
            .map((reviewerId) => ({ flagId, reviewerId })),
        )
  const failed = bulk.data?.results.filter((r) => r.status === 'failed') ?? []
  const flagCount = flags.length

  const onAssign = () => {
    if (rows.length === 0) return
    bulk.mutate(
      { rows, dry_run: false },
      {
        // Clean run → close. Partial failure → keep the dialog open and remember the pairs that
        // succeeded (excluded from `rows` above), so a retry re-posts only the failures, not 409s.
        onSuccess: (res) => {
          if (res.failed === 0) {
            onOpenChange(false)
            return
          }
          const ok = res.results.filter((r) => r.status === 'ok').map((r) => r.row_key)
          if (ok.length > 0) setDoneKeys((prev) => new Set([...prev, ...ok]))
        },
      },
    )
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={flagCount > 1 ? `Assign reviewers to ${flagCount} flags` : 'Assign reviewers'}
      onOpen={() => {
        setReviewerIds([])
        setSearch('')
        setDoneKeys(new Set())
        setSeenEmails({})
        bulk.reset() // clear a prior batch's summary so it doesn't bleed into a fresh open
      }}
    >
      <div className="space-y-4">
        <MultiSearchSelect
          label="Reviewers"
          htmlFor="reviewers"
          value={reviewerIds}
          onChange={setReviewerIds}
          onSearchChange={setSearch}
          options={pool.options}
          isPending={pool.isPending}
          isError={pool.isError}
          emptyLabel="No eligible reviewers to add."
        />
        {rows.length > 0 && (
          <p className="text-muted-foreground text-sm">
            Will create {rows.length} assignment{rows.length === 1 ? '' : 's'} across {flagCount}{' '}
            flag
            {flagCount === 1 ? '' : 's'}.
          </p>
        )}
        {skipped.length > 0 && (
          <div className="text-muted-foreground rounded-md border p-2 text-xs">
            <p className="mb-1">
              {skipped.length} pair{skipped.length === 1 ? '' : 's'} skipped — reviewer not in the
              candidate list for that flag:
            </p>
            <ul className="space-y-0.5">
              {skipped.map(({ flagId, reviewerId }) => (
                <li key={`${flagId}:${reviewerId}`}>
                  {emailFor(reviewerId)} → {labelByFlag.get(flagId) ?? flagId}
                </li>
              ))}
            </ul>
          </div>
        )}
        {bulk.data && failed.length > 0 && (
          <div className="border-destructive/40 rounded-md border p-2 text-xs">
            <p className="mb-1 font-medium">
              {bulk.data.succeeded} of {bulk.data.total} succeeded, {bulk.data.failed} failed.
            </p>
            <ul className="text-muted-foreground space-y-0.5">
              {failed.map((r) => {
                const row = bulk.variables?.rows.find((x) => x.row_key === r.row_key)
                const who = row ? emailFor(row.data.reviewer_id) : r.row_key
                const flagLabel = row
                  ? (labelByFlag.get(row.data.message_flag_id) ?? row.data.message_flag_id)
                  : ''
                return (
                  <li key={r.row_key}>
                    {who} → {flagLabel}: {r.error?.detail ?? r.error?.title ?? 'failed'}
                  </li>
                )
              })}
            </ul>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={rows.length === 0 || bulk.isPending} onClick={onAssign}>
            {bulk.isPending ? 'Assigning…' : `Assign${rows.length ? ` (${rows.length})` : ''}`}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
