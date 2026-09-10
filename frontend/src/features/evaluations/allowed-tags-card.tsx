import { useState } from 'react'
import { X } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, fieldErrorsFromProblem } from '@/lib/api/problem'
import { MAX_TAGS, MAX_TAG_KEY_LEN } from '@/lib/api/limits'
import { useEvaluationTagKeys } from './queries'
import { useAddTagKey, useRemoveTagKey, useUpdateEvaluation } from './mutations'

// The admin-defined allowed conversation-tag keys for an evaluation. Restriction is an explicit
// toggle (`restricted`): off → tags are unrestricted (any key); on → tags must use the listed keys
// (and an empty list while restricted forbids all tags).
export function AllowedTagsCard({
  evaluationId,
  restricted,
  canManage,
}: {
  evaluationId: string
  restricted: boolean
  canManage: boolean
}) {
  const keys = useEvaluationTagKeys(evaluationId)
  const add = useAddTagKey(evaluationId)
  const remove = useRemoveTagKey(evaluationId)
  const update = useUpdateEvaluation(evaluationId)
  const [key, setKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  // The key pending removal, or null. Removing one is as consequential as this page's other deletes:
  // while restriction is on it forbids the key at once, and conversations carrying it stop folding it
  // into the prompt — so it goes through the same confirmation they do.
  const [removing, setRemoving] = useState<string | null>(null)

  const rows = keys.data ?? []
  const atKeyCap = rows.length >= MAX_TAGS

  const submit = async () => {
    const trimmed = key.trim()
    // Every refusal the button makes, because the Enter path does not go through it: a second Add in
    // flight (two quick Enters used to POST twice, the second answered 409), or one past the server's cap.
    if (!trimmed || add.isPending || atKeyCap) return
    setError(null)
    try {
      await add.mutateAsync(trimmed)
      setKey('')
    } catch (err) {
      // 422 (bad key format) surfaces inline; 409 (duplicate) etc. are toasted globally.
      setError(err instanceof ApiError ? (fieldErrorsFromProblem(err.problem).key ?? null) : null)
    }
  }

  // `errorUpdateCount`, not `isError`: a read with no data drops both `error` and `isError` the moment
  // a refetch starts, so a block keyed on either unmounts on the very click that triggers the retry —
  // taking the button, its label and the keyboard's focus with it. The count survives the in-flight
  // attempt (it only ever increments), and once an attempt succeeds `data` carries the list, so the
  // `!keys.data` half is what closes this state.
  const readFailed = keys.errorUpdateCount > 0
  return (
    <Card>
      <CardHeader>
        {/* Countless until the query settles: "(0)" beside "Loading allowed keys…" reads as a loaded
            empty set, which is a different state and the one an admin would act on. */}
        <CardTitle>Allowed tags{keys.isSuccess ? ` (${rows.length})` : ''}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {canManage ? (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="size-4"
              checked={restricted}
              disabled={update.isPending}
              onChange={(e) => update.mutate({ tags_restricted: e.target.checked })}
            />
            Restrict tags to allowed keys
          </label>
        ) : (
          <p className="text-sm">
            {restricted ? 'Tags restricted to allowed keys.' : 'Tags unrestricted.'}
          </p>
        )}
        {/* Three states, not two: a failed read rendered as "Loading…" is the one that gets acted on,
            because the decision this card supports is whether to keep the restriction — and "still
            loading" invites unchecking it on the basis of a list that never arrived. `role="status"` so
            the failure *and* the recovery are announced, and Retry stays mounted while it refetches
            (labelled "Retrying…") instead of going inert with no feedback. The
            branch does not depend on `restricted` — the read that failed is the key list, which this
            card manages either way — and cached keys stay visible with copy that says they may be stale. */}
        <div role="status">
          {readFailed && !keys.data ? (
            <div className="flex items-center gap-2">
              <p className="text-destructive text-sm">
                {keys.isFetching ? 'Retrying the allowed keys…' : "Couldn't load the allowed keys."}
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={keys.isFetching}
                onClick={() => void keys.refetch()}
              >
                {keys.isFetching ? 'Retrying…' : 'Retry'}
              </Button>
            </div>
          ) : keys.isError && keys.data ? (
            // Cached keys are still rendered below, so this says they may be stale rather than claiming
            // nothing loaded — the ambiguity an admin would otherwise resolve by re-adding a key. It
            // keeps a Retry for the same reason the no-data branch does: with `refetchOnWindowFocus`
            // off and a 30s `staleTime`, nothing else in this view will try again.
            <div className="flex items-center gap-2">
              <p className="text-destructive text-sm">
                Couldn't refresh the allowed keys — showing the last ones loaded.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={keys.isFetching}
                onClick={() => void keys.refetch()}
              >
                {keys.isFetching ? 'Retrying…' : 'Retry'}
              </Button>
            </div>
          ) : (
            <p className="text-muted-foreground text-xs">
              {!restricted
                ? 'No restriction — any tag key is allowed.'
                : keys.isPending
                  ? 'Loading allowed keys…'
                  : rows.length === 0
                    ? 'Restricted with no keys — no conversation tags are allowed.'
                    : 'Conversation tags are restricted to these keys.'}
            </p>
          )}
        </div>
        {rows.length > 0 && (
          <div className="flex flex-wrap gap-1.5" data-testid="allowed-tag-keys">
            {rows.map((row) => (
              <Badge key={row.id} variant="tag" className="gap-1">
                {row.key}
                {canManage && (
                  <button
                    type="button"
                    aria-label={`Remove ${row.key}`}
                    className="opacity-70 hover:opacity-100"
                    disabled={remove.isPending}
                    onClick={() => setRemoving(row.key)}
                  >
                    <X className="size-3" />
                  </button>
                )}
              </Badge>
            ))}
          </div>
        )}
        {canManage && (
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <Input
                aria-label="New tag key"
                maxLength={MAX_TAG_KEY_LEN}
                value={key}
                onChange={(e) => {
                  // The message describes the key that was submitted; editing it makes it stale.
                  setError(null)
                  setKey(e.target.value)
                }}
                placeholder="e.g. env"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    void submit()
                  }
                }}
              />
              <Button
                type="button"
                size="sm"
                disabled={add.isPending || !key.trim() || atKeyCap}
                onClick={submit}
              >
                Add
              </Button>
            </div>
            {error && (
              <p role="alert" className="text-destructive text-sm">
                {error}
              </p>
            )}
            {atKeyCap && (
              <p className="text-muted-foreground text-xs">
                Up to {MAX_TAGS} allowed keys per evaluation.
              </p>
            )}
          </div>
        )}
      </CardContent>

      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null)
        }}
        title="Remove allowed tag key"
        description={
          restricted
            ? `Remove "${removing}" from the allowed keys? Conversations already tagged with it keep the tag on the record, but it stops being sent to the model and can't be set again until the key is re-added.`
            : `Remove "${removing}" from the allowed keys? Tags are unrestricted right now, so nothing changes for existing conversations until the restriction is turned on.`
        }
        confirmLabel="Remove"
        destructive
        pending={remove.isPending}
        onConfirm={() => {
          const key = removing
          if (key === null) return
          // Closed on success, not before the request: closing first leaves `pending` — and so the
          // confirm's in-flight state and its held Esc — unreachable, and a failed DELETE loses the dialog
          // that named the key. Matches this page's other destructive confirms.
          remove.mutate(key, { onSuccess: () => setRemoving(null) })
        }}
      />
    </Card>
  )
}
