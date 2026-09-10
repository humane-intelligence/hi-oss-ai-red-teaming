import { useEffect, useId, useRef, useState } from 'react'
import { Loader2, Tag, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'
import { TagKeyField } from './tag-key-field'
import type { AllowedKeysStatus } from './tag-key-field'
import { tagRowStats, tagsFromRows, type TagRow } from './tag-rows'
import { MAX_TAGS, MAX_TAG_KEY_LEN, MAX_TAG_VALUE_LEN } from '@/lib/api/limits'
import { Textarea } from '@/components/ui/textarea'
import { AttachmentStrip } from './attachment-strip'
import { useMessageAttachments } from './use-message-attachments'
import type { UserMessageIn } from '@/lib/api/types'

// The message composer shared by the single-conversation view and each side-by-side pane:
// owns the input + attachment state and the send/clear/restore flow, so both surfaces behave
// identically (and the image-on-send handling lives in one place). The surrounding chrome
// (border, padding) stays with the caller.
export function ChatComposer({
  send,
  pending,
  disabled = false,
  acceptsImages,
  tagsEnabled,
  allowedTagKeys,
  allowedKeysStatus,
  placeholder,
  ariaLabel = 'Message',
  rows = 2,
  size = 'default',
  autoFocusKey,
}: {
  send: (content: string, imageKeys?: string[], tags?: UserMessageIn['tags']) => Promise<boolean>
  pending: boolean
  disabled?: boolean
  acceptsImages: boolean
  // Whether the parent evaluation allows tags at all; false renders no tag editor (the backend
  // rejects tagged writes there, so an editor could only produce a 400).
  tagsEnabled: boolean
  // The evaluation's allowed message-tag keys when it restricts tags, else `null` (free-form).
  allowedTagKeys: string[] | null
  // See `AllowedKeysStatus`: an unsettled allow-list query must not read as "nothing allowed", and
  // the two unsettled states need copy of their own rather than a blank picker.
  allowedKeysStatus: AllowedKeysStatus
  placeholder: string
  ariaLabel?: string
  rows?: number
  size?: 'default' | 'sm'
  // Focus the textarea on mount and whenever this changes (e.g. the open conversation id).
  autoFocusKey?: string
}) {
  const [input, setInput] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const attachments = useMessageAttachments()
  const [tagsOpen, setTagsOpen] = useState(false)
  const [tagRows, setTagRows] = useState<TagRow[]>([])
  const [tagError, setTagError] = useState<string | null>(null)
  // Row identity has to survive a removal (same reason as the Edit-tags dialog): keyed by index,
  // React re-points the surviving inputs at the next row's data and the caret jumps a row.
  const nextRowId = useRef(0)
  const blankRow = () => ({ id: `row-${nextRowId.current++}`, key: '', value: '' })
  const { tagCount, hasBlankRow } = tagRowStats(tagRows)
  // While restricted there is nothing left to add once every allowed key is on a row — but only a
  // settled list can say that: `[].every()` is vacuously true, so a pending or failed query would
  // otherwise disable "Add tag" as if every key were taken.
  const noKeyLeft =
    allowedTagKeys !== null &&
    allowedKeysStatus === 'success' &&
    allowedTagKeys.every((key) => tagRows.some((r) => r.key === key))
  const atCap = tagCount >= MAX_TAGS
  // Refusing another row is fine; refusing it silently is not. Each reason gets its own copy, except
  // the two allow-list states, whose own line already sits above the rows — the button points at it.
  const keysUnsettled = allowedTagKeys !== null && allowedKeysStatus !== 'success'
  // Split out from `noKeyLeft`, which is vacuously true for an empty allow-list: "every key is already
  // used" about an evaluation that allows none is the same false reason the unsettled case had. Its own
  // line already sits above the rows, so the button points at it.
  const noKeysAllowed =
    allowedTagKeys !== null && allowedKeysStatus === 'success' && allowedTagKeys.length === 0
  const addBlocked = atCap || noKeyLeft || keysUnsettled || hasBlankRow
  // Editing tags mid-stream can't reach the in-flight turn, so the panel goes read-only with the
  // rest of the composer — the same rule `AttachmentStrip` follows.
  const tagsLocked = pending || disabled
  // Suppressed while the panel is read-only: `tagsLocked` disables everything for a turn in flight, and
  // announcing "fill the empty row first" then would describe a refusal the operator cannot act on.
  const addBlockedReason = tagsLocked
    ? null
    : atCap
      ? `Up to ${MAX_TAGS} tags per message`
      : noKeysAllowed || keysUnsettled
        ? 'See the note above'
        : noKeyLeft
          ? 'Every key this evaluation allows is already used'
          : hasBlankRow
            ? 'Fill the empty row first'
            : null
  const tagPanelId = `${useId()}-tags`
  const addReasonId = `${tagPanelId}-add-reason`

  useEffect(() => {
    if (autoFocusKey !== undefined) inputRef.current?.focus()
  }, [autoFocusKey])

  const doSend = async () => {
    const content = input.trim()
    if (!content || pending || disabled) return
    if (attachments.uploading || attachments.failed) return
    const imageKeys = attachments.keys
    const staged = attachments.attachments
    const stagedTags = tagRows
    // A row the author typed a value into but left keyless, or a duplicate key, would vanish into
    // the map — and the message persists immutably, so there is no fixing it after the fact.
    const built = tagsFromRows(tagRows)
    if (!built.ok) {
      setTagError(built.error)
      setTagsOpen(true)
      return
    }
    const tags = built.tags
    setTagError(null)
    setInput('')
    // The images/tags ride the optimistic send now — clear immediately rather than leaving
    // them staged until the reply finishes.
    attachments.clear()
    setTagRows([])
    setTagsOpen(false)
    inputRef.current?.focus()
    const ok = await send(content, imageKeys, tags)
    if (!ok) {
      // Send failed before the turn was persisted — put the text, attachments, and tags back.
      setInput(content)
      attachments.restore(staged)
      setTagRows(stagedTags)
      setTagsOpen(stagedTags.length > 0)
    }
  }

  return (
    <>
      {acceptsImages && (
        <AttachmentStrip
          attachments={attachments.attachments}
          onAdd={attachments.add}
          onRemove={attachments.remove}
          disabled={pending || disabled}
        />
      )}
      {tagsEnabled && (
        <div className="mt-2">
          <button
            type="button"
            aria-expanded={tagsOpen}
            aria-controls={tagPanelId}
            onClick={() =>
              setTagsOpen((open) => {
                const next = !open
                if (next && tagRows.length === 0) setTagRows([blankRow()])
                return next
              })
            }
            // The padding is the hit box: bare text would leave this under the 24px minimum, and on a
            // phone this is the only way into the tagging surface.
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 py-1.5 text-xs"
          >
            <Tag className="size-3" /> Tags{tagCount ? ` (${tagCount})` : ''}
          </button>
          {tagsOpen && (
            <div id={tagPanelId} className="mt-1 space-y-1">
              {tagError && <p className="text-destructive text-xs">{tagError}</p>}
              {allowedTagKeys !== null && allowedKeysStatus === 'pending' && (
                <p className="text-muted-foreground text-xs">Loading allowed keys…</p>
              )}
              {allowedTagKeys !== null && allowedKeysStatus === 'error' && (
                <p className="text-destructive text-xs">
                  Couldn't load the allowed keys — reload the page to try again.
                </p>
              )}
              {allowedKeysStatus === 'success' && allowedTagKeys?.length === 0 && (
                <p className="text-muted-foreground text-xs">
                  This evaluation restricts tags and allows no keys yet.
                </p>
              )}
              {tagRows.map((row, i) => (
                <div key={row.id} className="flex items-center gap-1">
                  <TagKeyField
                    ariaLabel={`Message tag ${i + 1} key`}
                    value={row.key}
                    onChange={(key) => {
                      setTagError(null)
                      setTagRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, key } : r)))
                    }}
                    allowedKeys={allowedTagKeys}
                    keysStatus={allowedKeysStatus}
                    takenKeys={tagRows.filter((_, idx) => idx !== i).map((r) => r.key)}
                    disabled={tagsLocked}
                    maxLength={MAX_TAG_KEY_LEN}
                    className="h-8"
                  />
                  <Input
                    aria-label={`Message tag ${i + 1} value`}
                    value={row.value}
                    onChange={(e) => {
                      setTagError(null)
                      setTagRows((rows) =>
                        rows.map((r, idx) => (idx === i ? { ...r, value: e.target.value } : r)),
                      )
                    }}
                    placeholder="value"
                    disabled={tagsLocked}
                    maxLength={MAX_TAG_VALUE_LEN}
                    className="h-8"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    // `shrink-0` keeps the 36px hit box: `min-width: auto` would let this row squeeze the
                    // button down to its icon.
                    className="shrink-0"
                    aria-label={`Remove message tag ${i + 1}`}
                    disabled={tagsLocked}
                    onClick={() => {
                      setTagError(null)
                      setTagRows((rows) => rows.filter((_, idx) => idx !== i))
                    }}
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
              <div className="flex items-center gap-2">
                {/* Same contract as the Edit-tags dialog, which authors the same map: `aria-disabled`
                    rather than `disabled` so the control stays in the tab order and can carry the
                    reason (a disabled button shows no `title` — `Button` sets
                    `disabled:pointer-events-none`), and `tagsLocked` stays a hard `disabled` because
                    a turn in flight is the one state where the whole panel is read-only. */}
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={tagsLocked}
                  aria-disabled={addBlocked || undefined}
                  aria-describedby={addBlockedReason ? addReasonId : undefined}
                  className={cn(addBlocked && 'text-muted-foreground')}
                  onClick={() => {
                    if (addBlocked) return
                    // Clearing the error too: it names a row by index, so adding one shifts what the
                    // message points at (the dialog clears on every row edit for the same reason).
                    setTagError(null)
                    setTagRows((rows) => [...rows, blankRow()])
                  }}
                >
                  Add tag
                </Button>
                {addBlockedReason && (
                  <p id={addReasonId} className="text-muted-foreground text-xs">
                    {addBlockedReason}
                  </p>
                )}
              </div>
            </div>
          )}
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void doSend()
        }}
        className="mt-2 flex gap-2"
      >
        <Textarea
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void doSend()
            }
          }}
          aria-label={ariaLabel}
          placeholder={placeholder}
          rows={rows}
          className="min-h-0"
        />
        <Button
          type="submit"
          size={size}
          disabled={
            pending || !input.trim() || disabled || attachments.uploading || attachments.failed
          }
          title={
            attachments.failed
              ? 'Remove the failed attachment first'
              : attachments.uploading
                ? 'Waiting for attachments to upload…'
                : undefined
          }
        >
          {pending ? <Loader2 className="size-4 animate-spin" /> : 'Send'}
        </Button>
      </form>
    </>
  )
}
