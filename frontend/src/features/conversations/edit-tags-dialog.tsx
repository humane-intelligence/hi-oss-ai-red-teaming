import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'
import { Modal } from '@/components/ui/modal'
import { ApiError, fieldErrorsFromProblem, humanizeError } from '@/lib/api/problem'
import { MAX_TAGS, MAX_TAG_KEY_LEN, MAX_TAG_VALUE_LEN } from '@/lib/api/limits'
import { useUpdateConversationTags } from './mutations'
import { TagKeyField } from './tag-key-field'
import type { AllowedKeysStatus } from './tag-key-field'
import { tagRowStats, tagsFromRows, type TagMap, type TagRow } from './tag-rows'

export function EditTagsDialog({
  evaluationId,
  conversationId,
  currentTags,
  allowedKeys,
  keysStatus,
  open,
  onOpenChange,
}: {
  evaluationId: string
  conversationId: string
  currentTags: TagMap
  // The evaluation's allowed keys when it restricts tags, else `null` (free-form). Advertised here
  // because a key outside the set is a 400 the author can only guess at otherwise.
  allowedKeys: string[] | null
  keysStatus: AllowedKeysStatus
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const update = useUpdateConversationTags(evaluationId)
  // `null` = no local edits yet, so the rows follow `currentTags`. Since the PATCH writes the whole
  // map, rows pinned at open time would silently drop a tag a refetch brought in (another tab, the
  // same conversation); tracking edits instead of snapshotting keeps an untouched dialog current.
  // Once the user types, their draft wins — the write is last-one-in by design.
  const [rows, setRows] = useState<TagRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const addReasonId = `${useId()}-add-tag-reason`
  // Row identity has to survive a removal: keyed by index, React re-points the surviving inputs at
  // the next row's data, so a caret sitting in row 2 lands in row 3 when row 1 goes.
  const nextRowId = useRef(0)
  const blankRow = () => ({ id: `new-${nextRowId.current++}`, key: '', value: '' })
  // Whether this dialog can still show a message of its own: mounted *and* open, since `Modal`
  // renders its children only while open. A layout effect, not a passive one: React flushes passive
  // effects on a scheduler task while a `mutateAsync` rejection lands in a microtask, so a parent
  // closing the dialog during a discrete event could leave this `true` and route the failure to a
  // surface that is already gone. Re-armed on every change of `open` — a ref cleared only in an
  // unmount cleanup would be stuck at `false` after StrictMode's mount/unmount/remount cycle.
  const reachable = useRef(open)
  useLayoutEffect(() => {
    reachable.current = open
    return () => {
      reachable.current = false
    }
  }, [open])
  // Order-independent identity of a tag map, so a re-keyed but equal map is not an edit.
  const asTagKey = (tags: TagMap) => JSON.stringify(Object.entries(tags).sort())

  const serverRows = useMemo<TagRow[]>(() => {
    const entries = Object.entries(currentTags)
    // Ids derived from the key, not a counter: they have to be stable across the moment local edits
    // materialise, or React would remount the inputs on the first keystroke and drop the caret.
    return entries.length > 0
      ? entries.map(([key, value]) => ({ id: `tag-${key}`, key, value }))
      : [{ id: 'tag-blank', key: '', value: '' }]
  }, [currentTags])
  const rowsShown = rows ?? serverRows
  // The cap and the counter read keyed rows: a seeded or added blank row is neither stored nor sent, so
  // counting rows would report "1/16 tags" over an empty map and refuse a legal tag at the cap. Shared
  // with the composer, which authors the same map from the same kind of rows.
  const { tagCount, hasBlankRow } = tagRowStats(rowsShown)
  const keysSettled = keysStatus === 'success'
  // Five distinct reasons the dialog can refuse another row, each with its own copy below. The
  // allow-list ones need a settled list: `[].every()` is vacuously true, so an unsettled query would
  // otherwise read as "every allowed key is already used".
  const atCap = tagCount >= MAX_TAGS

  const keysExhausted =
    allowedKeys !== null &&
    keysSettled &&
    allowedKeys.length > 0 &&
    allowedKeys.every((key) => rowsShown.some((r) => r.key === key))
  const noKeysAllowed = allowedKeys !== null && keysSettled && allowedKeys.length === 0
  // Until the allow-list settles a new row's picker has nothing in it, so adding one produces a row
  // the operator cannot fill and cannot explain. The loading/error line above is the reason.
  const keysUnsettled = allowedKeys !== null && !keysSettled
  const addBlocked = atCap || keysExhausted || noKeysAllowed || keysUnsettled || hasBlankRow
  // Shown as copy, not as `title`: `Button` carries `disabled:pointer-events-none`, so a disabled
  // button never receives the hover that would render a tooltip.
  // The two allow-list states already have their own copy above, so the button points at it instead of
  // repeating it — one sentence per state on screen, not two.
  const addBlockedReason = atCap
    ? `Up to ${MAX_TAGS} tags per conversation`
    : keysExhausted
      ? 'Every key this evaluation allows is already used'
      : noKeysAllowed || keysUnsettled
        ? 'See the note above'
        : hasBlankRow
          ? 'Fill the empty row first'
          : null
  // Two properties at once. Untouched rows are the server's, so `rows === null` is never dirty —
  // that is what keeps a background refetch from reading as an edit. Once touched, the comparison is
  // between the map that would be *sent* and the stored one, not between raw rows: trimming a key or
  // leaving an added row blank normalises away, and PATCHing an identical map back is pure waste.
  // Rows that cannot build stay dirty, or Save would be disabled with no way to surface the reason.
  const built = tagsFromRows(rowsShown, currentTags)
  const dirty = rows !== null && (!built.ok || asTagKey(built.tags) !== asTagKey(currentTags))

  const seed = () => {
    // Drop local edits each open so stale rows from a prior edit never linger.
    setRows(null)
    setError(null)
  }

  // Every edit clears the message: it names the rows that were submitted, so editing them makes it
  // stale — a key the operator has already replaced would still be reported as the offender.
  const setRow = (i: number, patch: Partial<TagRow>) => {
    setError(null)
    setRows(rowsShown.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))
  }
  const addRow = () => {
    setError(null)
    setRows([...rowsShown, blankRow()])
  }
  const removeRow = (i: number) => {
    setError(null)
    setRows(rowsShown.filter((_, idx) => idx !== i))
  }

  const submit = async () => {
    setError(null)
    // A key-less row with something typed in it, or a key that collides after trimming, would be
    // dropped or overwritten on the way into the map — silently losing what the operator wrote.
    if (!built.ok) {
      setError(built.error)
      return
    }
    const tags = built.tags
    try {
      await update.mutateAsync({ conversationId, tags })
      onOpenChange(false)
    } catch (err) {
      // This dialog is the only channel for its own failures (the mutation opts out of the global
      // toast), so everything lands here: the field reason when the body names one, otherwise
      // `humanizeError` — which already returns `problem.detail` below 500 (so the policy 400 still
      // names the offending keys) while keeping the curated copy for 403/404 and staying generic on
      // 5xx. Reading `detail` ahead of it would defeat exactly those three.
      const message =
        err instanceof ApiError
          ? (fieldErrorsFromProblem(err.problem).tags ?? humanizeError(err))
          : humanizeError(err)
      // Esc is held while the write is in flight, but a route change unmounts the dialog and the parent
      // can still close it — and an inline error on a surface nobody is looking at reports the failed
      // write to nobody, because this mutation opts out of the global toast.
      if (reachable.current) setError(message)
      else toast.error(message)
    }
  }

  const dialogTitle = Object.keys(currentTags).length > 0 ? 'Edit tags' : 'Add tags'
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={dialogTitle}
      onOpen={seed}
      busy={update.isPending}
    >
      <div className="space-y-4">
        <p className="text-muted-foreground text-sm">
          {allowedKeys === null
            ? 'Free-form key/value tags stored on the conversation and folded into the model’s context alongside the prompt, minus any the evaluation’s tagging policy no longer allows.'
            : 'Key/value tags stored on the conversation and folded into the model’s context alongside the prompt, minus any the evaluation’s tagging policy no longer allows. This evaluation restricts tags, so keys are picked from the ones it allows.'}
        </p>
        {allowedKeys !== null && keysStatus === 'pending' && (
          <p className="text-muted-foreground text-xs">Loading allowed keys…</p>
        )}
        {allowedKeys !== null && keysStatus === 'error' && (
          // A state readout, not an alert (the operator just opened this dialog and is reading it), and
          // it no longer says "retry": nothing here can refetch — the allow-list is the page's query.
          <p role="status" className="text-destructive text-sm">
            Couldn't load the allowed keys — reload the page to try again.
          </p>
        )}
        {keysStatus === 'success' && allowedKeys?.length === 0 && (
          <p className="text-muted-foreground text-xs">
            No keys are allowed yet — an evaluation admin has to add some before tags can be saved.
          </p>
        )}
        {/* `role="alert"`: this dialog opts the mutation out of the global toast, so a failed Save is
            announced here or nowhere. Without it the interaction is indistinguishable from a no-op for
            a screen-reader user, whose focus stays on the button while the message lands elsewhere. */}
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
        {/* The row list scrolls instead of the dialog: at the cap the footer would otherwise sit far
            below the fold. Viewport-relative so a laptop gets fewer rows rather than a clipped Save. */}
        <div className="max-h-[min(26rem,50svh)] space-y-2 overflow-y-auto pr-1">
          {rowsShown.map((row, i) => (
            <div key={row.id} className="flex items-center gap-2">
              <TagKeyField
                ariaLabel={`Tag ${i + 1} key`}
                autoFocus={i === 0}
                maxLength={MAX_TAG_KEY_LEN}
                value={row.key}
                onChange={(key) => setRow(i, { key })}
                allowedKeys={allowedKeys}
                keysStatus={keysStatus}
                takenKeys={rowsShown.filter((_, idx) => idx !== i).map((r) => r.key)}
              />
              <Input
                aria-label={`Tag ${i + 1} value`}
                maxLength={MAX_TAG_VALUE_LEN}
                value={row.value}
                onChange={(e) => setRow(i, { value: e.target.value })}
                placeholder="value"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                // `shrink-0` keeps the 36px hit box `size-icon` sets: `min-width: auto` only stops this
                // row shrinking the button past its icon.
                className="shrink-0"
                aria-label={`Remove tag ${i + 1}`}
                onClick={() => removeRow(i)}
              >
                <X className="size-4" />
              </Button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {/* `aria-disabled`, not `disabled`: a natively disabled button leaves the tab order, so a
              keyboard user never reaches it and never hears why it is refused. The handler no-ops
              instead, and the reason is wired as the description rather than a `title` (which a
              disabled control can't show at all — `Button` sets `disabled:pointer-events-none`). */}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              if (addBlocked) return
              addRow()
            }}
            aria-disabled={addBlocked || undefined}
            aria-describedby={addBlockedReason ? addReasonId : undefined}
            // Muted rather than translucent: `opacity` on text is what the chip rationale rejects, and this
            // control stays in the tab order, so it has to stay readable.
            className={cn(addBlocked && 'text-muted-foreground')}
          >
            Add tag
          </Button>
          {addBlockedReason && (
            <p id={addReasonId} className="text-muted-foreground text-xs">
              {addBlockedReason}
            </p>
          )}
        </div>
        {/* Beside the button, not inside it: in the button the count joins the accessible name
            ("Add tag1/16") and the control gets hard to address. */}
        <p className="text-muted-foreground text-xs tabular-nums">
          {tagCount}/{MAX_TAGS} tags
        </p>
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={update.isPending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button type="button" disabled={update.isPending || !dirty} onClick={submit}>
            {update.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
