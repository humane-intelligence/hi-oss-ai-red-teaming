import { useLayoutEffect, useRef, useState } from 'react'
import { Pencil, X } from 'lucide-react'
import type { NoteResponse } from '@/lib/api/types'

type Props = {
  note: NoteResponse
  /** Set only when the note is someone else's — i.e. an `evaluation_groups:manage` read. */
  authorLabel?: string
  onEdit?: () => void
  onDelete?: () => void
}

export function NoteCard({ note, authorLabel, onEdit, onDelete }: Props) {
  const [expanded, setExpanded] = useState(false)
  const covers = note.message_ids.length
  // Only the author edits or deletes: a break-glass reader sees someone else's note
  // (`authorLabel` set) and the backend would let them rewrite it, which is a product
  // decision this slice does not take.
  const mine = authorLabel === undefined
  const written = new Date(note.created_at).toLocaleString()
  // `line-clamp-6` clamps *visual* lines, so whether a note is truncated depends on how its text
  // wraps at the current width — not on its length or its newline count, each of which lets a
  // clipped note through. Measured, and re-measured on resize: the same note is clipped at 375 and
  // whole at 1280. While expanded the clamp is off, so the last measurement stands.
  const bodyRef = useRef<HTMLParagraphElement>(null)
  const [clipped, setClipped] = useState(false)
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!el || expanded) return
    const measure = () => setClipped(el.scrollHeight > el.clientHeight)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [expanded, note.text])

  return (
    <div className="bg-card rounded-md border px-2 py-1.5 text-sm">
      <div className="text-muted-foreground mb-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <span>{authorLabel ?? 'You'}</span>
        <span>·</span>
        <time dateTime={note.created_at}>{written}</time>
        {covers > 1 && (
          <span className="bg-muted rounded px-1.5 py-0.5">Covers {covers} messages</span>
        )}
        {/* Always visible rather than hover-revealed, and deliberately not `ml-auto`: the card's
            right edge is the first thing clipped when an ancestor overflows. Labels carry the
            timestamp because one row can hold several notes. */}
        {mine && (onEdit || onDelete) && (
          <span className="flex items-center gap-1">
            {onEdit && (
              <button
                type="button"
                onClick={onEdit}
                aria-label={`Edit your note from ${written}`}
                title="Edit note"
                className="hover:bg-muted hover:text-foreground rounded p-1.5"
              >
                <Pencil className="size-3.5" />
              </button>
            )}
            {onDelete && (
              <button
                type="button"
                onClick={onDelete}
                aria-label={`Delete your note from ${written}`}
                title="Delete note"
                className="hover:bg-muted hover:text-destructive rounded p-1.5"
              >
                <X className="size-3.5" />
              </button>
            )}
          </span>
        )}
      </div>
      {/* Plain text, not Markdown: a note is not promised to be markdown, and a stray
          `#` or `-` would reflow the transcript around it. */}
      <p
        ref={bodyRef}
        id={`note-body-${note.id}`}
        className={
          expanded
            ? 'break-words whitespace-pre-wrap'
            : 'line-clamp-6 break-words whitespace-pre-wrap'
        }
      >
        {note.text}
      </p>
      {clipped && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={`note-body-${note.id}`}
          className="text-muted-foreground hover:text-foreground mt-0.5 text-xs underline underline-offset-2"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}
