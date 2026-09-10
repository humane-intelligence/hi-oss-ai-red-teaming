import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  StickyNote,
  Tag as TagIcon,
} from 'lucide-react'
import { useSubmission, useSubmissionMessages } from './queries'
import {
  useConversationNotes,
  useDeletedConversationNotes,
  NOTES_PAGE_SIZE,
} from '@/features/notes/queries'
import { indexNotes } from '@/features/notes/note-index'
import { NoteCard } from '@/features/notes/note-card'
import { WriteNoteDialog } from '@/features/notes/write-note-dialog'
import { useDeleteNote, useRestoreNote } from '@/features/notes/mutations'
import { noteErrorMessage } from '@/features/notes/stale-session'
import { useScenario } from '@/features/scenarios/queries'
import { useUnassignReviewer } from './mutations'
import { VerdictDialog } from './verdict-dialog'
import { AssignReviewerDialog } from './assign-reviewer-dialog'
import { verdictSummary } from './format'
import { mergeTranscript, type TranscriptRow } from './transcript'
import { useUserLookup } from '@/features/users/queries'
import { humanizeError } from '@/lib/api/problem'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useAuth } from '@/lib/auth/auth-context'
import { RequirePermission } from '@/lib/auth/require-permission'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { PageHeader } from '@/components/shared/page-header'
import { ReviewProgress } from '@/components/shared/review-progress'
import { StatusPill } from '@/components/shared/status-pill'
import { Markdown } from '@/components/shared/markdown'
import { MessageAttachments } from '@/components/shared/message-attachments'
import { TagContext } from '@/components/shared/tag-context'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { NoteResponse, ReviewResponse } from '@/lib/api/types'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { LabelsTruncatedNote } from '@/features/annotations/labels-truncated-note'
import { AnnotateMessageDialog } from '@/features/annotations/annotate-message-dialog'
import { AnnotationChips } from '@/features/annotations/annotation-chips'
import { indexAnnotations } from '@/features/annotations/annotation-index'
import { useConversationAnnotations } from '@/features/annotations/queries'
import type { AnnotationResponse } from '@/lib/api/types'

const COMPLETED = new Set(['approved', 'rejected'])

type RowProps = {
  row: TranscriptRow
  notes: NoteResponse[]
  /** Another note anchored elsewhere also covers this row. */
  crossReferenced: boolean
  selected: boolean
  selectMode: boolean
  /** 1-based position in the transcript — the checkboxes are otherwise indistinguishable to AT. */
  rowNumber: number
  /** Absent when the caller can't write notes, or the row isn't a live message. */
  onToggle?: () => void
  authorLabel: (note: NoteResponse) => string | undefined
  /** Absent when the caller lacks the matching note key. */
  onEditNote?: (note: NoteResponse) => void
  onDeleteNote?: (note: NoteResponse) => void
  /** Every author's labels on this row — shared reads, unlike the notes above. */
  annotations: AnnotationResponse[]
  /** Absent when the caller lacks `annotations:create`. */
  onAnnotate?: () => void
}

function TranscriptRowView({
  row,
  notes,
  crossReferenced,
  selected,
  selectMode,
  rowNumber,
  onToggle,
  authorLabel,
  onEditNote,
  onDeleteNote,
  annotations,
  onAnnotate,
}: RowProps) {
  const { message: m, kind } = row
  return (
    <div
      id={kind !== 'context' ? `flagged-${m.id}` : undefined}
      className={cn(
        'group rounded-md border px-3 py-2',
        kind === 'flagged' && 'border-l-err bg-err/5 border-l-2',
        selected && 'ring-primary/30 ring-1',
      )}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        {onToggle && (
          <input
            type="checkbox"
            className={cn(
              'size-4 shrink-0 transition-opacity',
              // Hover-reveal is a mouse idiom: without the focus and coarse-pointer escapes a
              // keyboard user focuses an invisible control and a touch user never sees one at all,
              // and since the first tick is what enables `selectMode`, the feature is undiscoverable.
              selectMode || selected
                ? 'opacity-100'
                : 'opacity-0 group-hover:opacity-100 focus:opacity-100 [@media(hover:none)]:opacity-100',
            )}
            checked={selected}
            onChange={onToggle}
            aria-label={`Select ${m.role} message ${rowNumber} to add a note`}
          />
        )}
        <span
          className={cn(
            'text-muted-foreground text-xs font-medium tracking-wide uppercase',
            kind === 'superseded' && 'opacity-60',
          )}
        >
          {m.role}
          {m.slot ? ` · ${m.slot}` : ''}
        </span>
        {(kind === 'flagged' || kind === 'superseded') && <Badge variant="err">flagged</Badge>}
        {kind === 'superseded' && <Badge variant="neutral">superseded</Badge>}
        {crossReferenced && <Badge variant="neutral">note</Badge>}
      </div>
      {m.image_keys && m.image_keys.length > 0 && (
        <MessageAttachments imageKeys={m.image_keys} className="mb-1" />
      )}
      {/* Dim the stale text, not the row. The tag chip starts at 4.70:1, so inheriting a 0.6
          ancestor puts it at 2.23:1 (light) / 2.80:1 (dark) — under the 3.0 large-text floor, and
          the same reason the conversation header marks unsent tags instead of dimming them. Body
          text survives the dimming; the badges and the record do not, so they keep full opacity and
          the `superseded` badge is what names the state. */}
      <div className={cn(kind === 'superseded' && 'opacity-60')}>
        <Markdown content={m.content} />
      </div>
      <TagContext tagContext={m.tag_context} partial={m.tag_context_partial} messageId={m.id} />
      {(annotations.length > 0 || onAnnotate) && (
        <div className="mt-2 flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <AnnotationChips annotations={annotations} />
          </div>
          {onAnnotate && (
            <button
              type="button"
              onClick={onAnnotate}
              className="text-muted-foreground hover:text-foreground inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-xs"
            >
              <TagIcon className="size-3" aria-hidden />
              {annotations.length > 0 ? 'Edit labels' : 'Add label'}
              <span className="sr-only">{` on ${m.role} message ${rowNumber}`}</span>
            </button>
          )}
        </div>
      )}
      {notes.length > 0 && (
        <div data-testid={`notes-${m.id}`} className="mt-2 space-y-2 border-t pt-2">
          {/* First person only when every note here is the caller's: a break-glass reader
              (`evaluation_groups:manage`) sees other authors' notes in this same stack. */}
          <p className="text-muted-foreground text-xs font-medium">
            {notes.every((n) => authorLabel(n) === undefined) ? 'My notes' : 'Notes'}
          </p>
          {notes.map((note) => (
            <NoteCard
              key={note.id}
              note={note}
              authorLabel={authorLabel(note)}
              onEdit={onEditNote && (() => onEditNote(note))}
              onDelete={onDeleteNote && (() => onDeleteNote(note))}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function JumpToFlagged({ ids }: { ids: string[] }) {
  const [i, setI] = useState(-1)
  const go = (delta: number) => {
    const idx = (i + delta + ids.length) % ids.length
    setI(idx)
    document.getElementById(`flagged-${ids[idx]}`)?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'center',
    })
  }
  return (
    <div className="bg-card flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm">
      <span className="text-muted-foreground">
        {ids.length} flagged message{ids.length === 1 ? '' : 's'}
      </span>
      <div className="flex gap-1">
        <Button
          size="icon"
          variant="outline"
          className="size-7"
          aria-label="Previous flagged"
          onClick={() => go(-1)}
        >
          <ChevronUp className="size-4" />
        </Button>
        <Button
          size="icon"
          variant="outline"
          className="size-7"
          aria-label="Next flagged"
          onClick={() => go(1)}
        >
          <ChevronDown className="size-4" />
        </Button>
      </div>
    </div>
  )
}

function ReviewDetailContent() {
  const { submissionId = '' } = useParams<{ submissionId: string }>()
  const location = useLocation()
  const navigate = useNavigate()
  const subQ = useSubmission(submissionId)
  const messagesQ = useSubmissionMessages(submissionId)
  const submission = subQ.data?.submission
  const scenarioQ = useScenario(submission?.evaluation_id ?? '', submission?.scenario_id)
  const lookup = useUserLookup()
  const { has } = usePermissions()
  const { user } = useAuth()
  const unassign = useUnassignReviewer()
  const notesQ = useConversationNotes(submission?.conversation_id ?? '')
  const [verdictReview, setVerdictReview] = useState<ReviewResponse | null>(null)
  const [assignOpen, setAssignOpen] = useState(false)
  const [unassignReview, setUnassignReview] = useState<ReviewResponse | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [noteTargetIds, setNoteTargetIds] = useState<string[] | null>(null)
  const [editNote, setEditNote] = useState<NoteResponse | null>(null)
  const [deleteNote, setDeleteNote] = useState<NoteResponse | null>(null)
  const removeNote = useDeleteNote()
  const canWriteNote = has('notes:create')
  // Separate key from the notes above: an annotator holds both, the conversation's owner
  // reads annotations without either write key, and the red teamer holds none of it.
  const canAnnotate = has('annotations:create')
  const [annotateId, setAnnotateId] = useState<string | null>(null)
  // Every role holding `notes:create` also holds these two, so this changes nothing
  // today — it keeps the affordance tied to the key that authorises it.
  const canEditNote = has('notes:update')
  const canDeleteNote = has('notes:delete')

  // Only live messages may be noted — POST rejects an id the conversation no longer has,
  // so a superseded row can show a note but not join a new selection.
  const liveIds = new Set((messagesQ.data?.items ?? []).map((m) => m.id))
  const annotationsQ = useConversationAnnotations(submission?.conversation_id ?? '')
  // The selection is row-keyed and the transcript refetches, so prune ids that are gone —
  // otherwise a regenerate mid-selection posts an id the conversation dropped. Reconciled
  // during render, not in an effect (eslint bans setState-in-effect), and above the early
  // returns below so the hook order never changes. Same shape as users-list-page.
  const rowSig = [...liveIds].join(' ')
  const [seenSig, setSeenSig] = useState(rowSig)
  if (rowSig !== seenSig) {
    setSeenSig(rowSig)
    setSelected((prev) => {
      if (prev.length === 0) return prev
      const next = prev.filter((id) => liveIds.has(id))
      return next.length === prev.length ? prev : next
    })
  }
  const selectMode = selected.length > 0
  const toggleSelect = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const cameFromApp = location.key !== 'default'
  const goBack = () => (cameFromApp ? navigate(-1) : navigate('/reviews'))

  if (subQ.isPending) return <DetailSkeleton />
  if (subQ.isError || !subQ.data || !submission) {
    return (
      <div className="space-y-4">
        <p className="text-muted-foreground">
          {subQ.error ? humanizeError(subQ.error) : 'Submission not found.'}
        </p>
        <Link to="/reviews" className="text-sm underline-offset-2 hover:underline">
          ← Back to Reviews
        </Link>
      </div>
    )
  }

  const reviews = subQ.data.reviews.items
  const completed = reviews.filter((r) => COMPLETED.has(r.status)).length
  const scenario = scenarioQ.data
  const required = scenario?.required_reviews ?? 1
  // Be explicit about where the rigor comes from: a live challenge, a removed one,
  // or no challenge at all (free-form). The last two both default to 1 review.
  const rigorSource = scenario
    ? `challenge “${scenario.name}”`
    : submission.scenario_id != null
      ? 'challenge removed — default'
      : 'no challenge — default'

  const rows = mergeTranscript(
    messagesQ.data?.items ?? [],
    submission.messages,
    subQ.data.superseded_message_ids ?? [],
  )
  const flaggedTargets = rows.filter((r) => r.kind !== 'context').map((r) => r.message.id)
  const noteIndex = indexNotes(
    notesQ.data?.items ?? [],
    rows.map((r) => r.message.id),
  )
  // Over `rows`, not the live message list: `mergeTranscript` re-adds the flagged and
  // superseded messages the live list omits, so indexing on live ids would drop a rendered
  // row's chips *and* announce them as "not shown in this transcript" while they are on screen.
  const annotationIndex = indexAnnotations(
    annotationsQ.data?.items ?? [],
    rows.map((r) => r.message.id),
  )
  const authorLabel = (note: NoteResponse) =>
    note.created_by_id === user?.id ? undefined : lookup(note.created_by_id)
  // Drives the copy, not the data: a break-glass reader (`evaluation_groups:manage`) gets every
  // author's notes here, and first-person wording would then be a lie.
  const readsOtherAuthors = (notesQ.data?.items ?? []).some(
    (note) => note.created_by_id !== user?.id,
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Review submission"
        description={`Requires ${required} review${required === 1 ? '' : 's'} · ${rigorSource}`}
        breadcrumbs={
          <Breadcrumbs items={[{ label: 'Reviews', to: '/reviews' }, { label: 'Submission' }]} />
        }
      />

      <Button variant="ghost" size="sm" className="-ml-2 w-fit" onClick={goBack}>
        <ArrowLeft className="size-4" /> Back
      </Button>

      {/* `minmax(0,1fr)`: an `auto` track is sized to its content, which `min-w-0` cannot undo. */}
      {/* Two columns from `lg`, not `md`: at 768 the split left the review sidebar 213px. */}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-2">
        {/* Left: submission brief + full conversation transcript.
            `min-w-0`: a grid item defaults to min-width:auto and so refuses to shrink below its
            content's min-content width, which one unbroken long token is enough to blow past.
            `break-words` alone does not prevent that. */}
        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader>
              <div className="space-y-1">
                <div className="flex items-start gap-2">
                  <span className="font-medium">{submission.reason}</span>
                  {submission.red_flagged && <StatusPill status="red_flagged" />}
                </div>
                {submission.comment && (
                  <p className="text-muted-foreground text-sm">{submission.comment}</p>
                )}
                <p className="text-muted-foreground text-xs">
                  Flagged by {lookup(submission.created_by_id)} · {submission.messages.length}{' '}
                  flagged message{submission.messages.length === 1 ? '' : 's'}
                </p>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              {messagesQ.isError && (
                <p className="text-destructive text-sm">
                  Full transcript unavailable: {humanizeError(messagesQ.error)}
                </p>
              )}
              {notesQ.isError && (
                <p className="text-destructive text-sm">
                  Your notes are unavailable: {noteErrorMessage(notesQ.error)}
                </p>
              )}
              {annotationsQ.isError && (
                <p className="text-destructive text-sm">
                  Labels are unavailable: {humanizeError(annotationsQ.error)}
                </p>
              )}
              <LabelsTruncatedNote total={annotationsQ.data?.total} />
              {canWriteNote && (
                <p className="text-muted-foreground text-xs">
                  Select messages to add a note. Notes are visible to you and platform admins —
                  other reviewers don&apos;t see them.
                </p>
              )}
              {/* Mounted whenever the caller can write notes, not only while a selection exists: a
                  live region inserted together with its text is not reliably announced. */}
              {canWriteNote && (
                <span role="status" aria-live="polite" className="sr-only">
                  {selected.length > 0
                    ? `${selected.length} message${selected.length === 1 ? '' : 's'} selected`
                    : ''}
                </span>
              )}
              {selectMode && (
                // Sticky because "Add note" is the only way to act on a selection and the
                // transcript is taller than the viewport, so a selection made below the fold would
                // leave the action off-screen. `bg-card` rather than the tinted `bg-primary/5`:
                // a translucent bar lets the transcript scroll through it.
                <div className="border-primary/40 bg-card sticky top-0 z-20 flex items-center justify-between rounded-md border px-3 py-2 text-sm">
                  <span className="font-medium">{selected.length} selected</span>
                  <div className="flex gap-2">
                    <Button variant="ghost" size="sm" onClick={() => setSelected([])}>
                      Clear
                    </Button>
                    <Button size="sm" onClick={() => setNoteTargetIds(selected)}>
                      <StickyNote className="size-4" /> Add note on {selected.length}
                    </Button>
                  </div>
                </div>
              )}
              {messagesQ.isPending ? (
                <p className="text-muted-foreground text-sm">Loading transcript…</p>
              ) : rows.length === 0 ? (
                <p className="text-muted-foreground text-sm">No messages in this conversation.</p>
              ) : (
                rows.map((row, rowIndex) => (
                  <TranscriptRowView
                    key={row.message.id}
                    rowNumber={rowIndex + 1}
                    row={row}
                    notes={noteIndex.byAnchor.get(row.message.id) ?? []}
                    crossReferenced={noteIndex.crossReferenced.has(row.message.id)}
                    selected={selected.includes(row.message.id)}
                    selectMode={selectMode}
                    onToggle={
                      canWriteNote && liveIds.has(row.message.id)
                        ? () => toggleSelect(row.message.id)
                        : undefined
                    }
                    authorLabel={authorLabel}
                    onEditNote={canEditNote ? setEditNote : undefined}
                    onDeleteNote={canDeleteNote ? setDeleteNote : undefined}
                    annotations={annotationIndex.byMessage.get(row.message.id) ?? []}
                    onAnnotate={
                      canAnnotate && liveIds.has(row.message.id)
                        ? () => setAnnotateId(row.message.id)
                        : undefined
                    }
                  />
                ))
              )}
              {annotationIndex.unanchored.length > 0 && (
                <p className="text-muted-foreground rounded-md border border-dashed px-3 py-2 text-xs">
                  {annotationIndex.unanchored.length} label
                  {annotationIndex.unanchored.length === 1 ? '' : 's'} on messages not shown in this
                  transcript — a superseded reply keeps the labels it was given.
                </p>
              )}
              {noteIndex.unanchored.length > 0 && (
                <div className="space-y-2 rounded-md border border-dashed px-3 py-2">
                  <p className="text-muted-foreground text-xs">
                    {noteIndex.unanchored.length} note
                    {noteIndex.unanchored.length === 1 ? '' : 's'} on messages not shown in this
                    transcript
                  </p>
                  {noteIndex.unanchored.map((note) => (
                    <NoteCard
                      key={note.id}
                      note={note}
                      authorLabel={authorLabel(note)}
                      onEdit={canEditNote ? () => setEditNote(note) : undefined}
                      onDelete={canDeleteNote ? () => setDeleteNote(note) : undefined}
                    />
                  ))}
                </div>
              )}
              <DeletedNotes conversationId={submission.conversation_id} />
              {(notesQ.data?.total ?? 0) > NOTES_PAGE_SIZE && (
                <p className="text-muted-foreground text-xs">
                  {readsOtherAuthors
                    ? `Showing the ${NOTES_PAGE_SIZE} most recent notes of ${notesQ.data?.total}.`
                    : `Showing your ${NOTES_PAGE_SIZE} most recent notes of ${notesQ.data?.total}.`}
                </p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: review controls */}
        <div className="space-y-4">
          <div className="min-w-0 space-y-4 lg:sticky lg:top-6">
            {flaggedTargets.length > 0 && <JumpToFlagged ids={flaggedTargets} />}
            <Card>
              <CardHeader>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <ReviewProgress completed={completed} required={required} />
                  {has('reviews:create') && (
                    <Button size="sm" variant="outline" onClick={() => setAssignOpen(true)}>
                      Assign reviewer
                    </Button>
                  )}
                </div>
              </CardHeader>
              <CardContent className="space-y-2">
                {reviews.map((r) => (
                  <div
                    key={r.id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm"
                  >
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <StatusPill status={r.status} />
                        <span className="text-muted-foreground truncate">
                          {r.reviewer_email ?? lookup(r.reviewer_id)}
                        </span>
                      </div>
                      {r.status !== 'pending' && (
                        <div className="text-muted-foreground mt-1 text-xs">
                          {verdictSummary(r)}
                        </div>
                      )}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {has('reviews:update') && (
                        <Button size="sm" variant="outline" onClick={() => setVerdictReview(r)}>
                          {r.status === 'pending' ? 'Record verdict' : 'Edit verdict'}
                        </Button>
                      )}
                      {has('reviews:delete') && (
                        <Button size="sm" variant="outline" onClick={() => setUnassignReview(r)}>
                          Unassign
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
                {reviews.length === 0 && (
                  <p className="text-muted-foreground text-sm">No reviewers assigned yet.</p>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>

      <WriteNoteDialog
        conversationId={submission.conversation_id}
        // Re-filtered, not just captured at open: the transcript can refetch while the dialog is
        // up (a regenerate plus a window-focus refetch), and posting a dropped id is a 404. An
        // empty result leaves "Save note" disabled rather than sending a doomed request.
        messageIds={(noteTargetIds ?? []).filter((id) => liveIds.has(id))}
        open={noteTargetIds !== null}
        onOpenChange={(o) => {
          if (!o) setNoteTargetIds(null)
        }}
        onSuccess={() => setSelected([])}
      />

      <AnnotateMessageDialog
        messageId={annotateId ?? ''}
        conversationId={submission?.conversation_id ?? ''}
        annotations={annotationIndex.byMessage.get(annotateId ?? '') ?? []}
        currentUserId={user?.id}
        open={annotateId !== null}
        onOpenChange={(o) => {
          if (!o) setAnnotateId(null)
        }}
      />
      {/* The same form in edit mode — `conversationId`/`messageIds` are inert on that branch,
          which is why the note's own values are passed rather than the live selection. */}
      <WriteNoteDialog
        conversationId={editNote?.conversation_id ?? ''}
        messageIds={editNote?.message_ids ?? []}
        note={editNote ?? undefined}
        open={editNote !== null}
        onOpenChange={(o) => {
          if (!o) setEditNote(null)
        }}
      />
      <ConfirmDialog
        open={deleteNote !== null}
        onOpenChange={(o) => {
          if (!o) setDeleteNote(null)
        }}
        title="Delete note"
        description={
          deleteNote
            ? `Delete your note on ${deleteNote.message_ids.length} message${deleteNote.message_ids.length === 1 ? '' : 's'}? ${REVERSIBLE_DELETE_NOTE}`
            : ''
        }
        confirmLabel="Delete"
        destructive
        pending={removeNote.isPending}
        onConfirm={() => {
          if (!deleteNote) return
          removeNote.mutate(deleteNote.id, { onSuccess: () => setDeleteNote(null) })
        }}
      />
      <VerdictDialog review={verdictReview} onClose={() => setVerdictReview(null)} />
      <AssignReviewerDialog
        flags={[{ id: submission.id, label: submission.reason }]}
        open={assignOpen}
        onOpenChange={setAssignOpen}
      />
      <ConfirmDialog
        open={unassignReview !== null}
        onOpenChange={(o) => {
          if (!o) setUnassignReview(null)
        }}
        title="Unassign reviewer"
        description={`Remove this reviewer assignment? The review slot will be freed. ${REVERSIBLE_DELETE_NOTE} Restoring re-notifies the reviewer by email.`}
        confirmLabel="Unassign"
        destructive
        pending={unassign.isPending}
        onConfirm={() => {
          if (unassignReview) {
            unassign.mutate(unassignReview.id, { onSuccess: () => setUnassignReview(null) })
          }
        }}
      />
    </div>
  )
}

// Notes the caller deleted recently on this conversation, with the only action that
// reaches them once the delete toast is gone. Collapsed and unqueried until opened —
// the transcript card already fires several requests, and most reviews never need this.
function DeletedNotes({ conversationId }: { conversationId: string }) {
  const { has } = usePermissions()
  const [open, setOpen] = useState(false)
  const query = useDeletedConversationNotes(conversationId, open)
  const restore = useRestoreNote()
  const deleted = query.data?.items ?? []
  // Restoring needs `notes:delete`, so without it the browse surface is a dead end.
  if (!has('notes:delete')) return null

  return (
    <div className="rounded-md border border-dashed px-3 py-2">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls="deleted-notes"
        className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-xs font-medium"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        Recently deleted notes
        {open && !query.isPending && ` (${deleted.length})`}
      </button>
      {open && (
        <div id="deleted-notes" className="mt-2 space-y-2">
          <p className="text-muted-foreground text-xs">
            Notes you deleted recently can be restored here for a limited time.
          </p>
          {query.isPending && <p className="text-muted-foreground text-xs">Loading…</p>}
          {query.isError && (
            <p className="text-destructive text-xs">
              Could not load deleted notes: {noteErrorMessage(query.error)}
            </p>
          )}
          {!query.isPending && !query.isError && deleted.length === 0 && (
            <p className="text-muted-foreground text-xs">Nothing deleted recently.</p>
          )}
          {deleted.map((note) => (
            <div key={note.id} className="flex items-start justify-between gap-2">
              <p className="text-muted-foreground min-w-0 flex-1 truncate text-sm line-through">
                {note.text}
              </p>
              <Button
                variant="outline"
                size="sm"
                aria-label={`Restore note: ${note.text}`}
                disabled={restore.isPending}
                onClick={() => restore.mutate(note.id)}
              >
                Restore
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function ReviewDetailPage() {
  return (
    <RequirePermission
      anyOf={['reviews:read', 'reviews:update', 'reviews:delete', 'reviews:create']}
    >
      <ReviewDetailContent />
    </RequirePermission>
  )
}
