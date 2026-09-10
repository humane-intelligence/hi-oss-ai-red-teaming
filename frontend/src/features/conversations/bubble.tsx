import { useState } from 'react'
import { Ban as BanIcon, Check, Copy, Flag, Tag } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Markdown } from '@/components/shared/markdown'
import { Badge } from '@/components/ui/badge'
import { MessageAttachments } from '@/components/shared/message-attachments'
import { MessageMeta } from '@/components/shared/message-meta'
import { TagContext } from '@/components/shared/tag-context'
import { AnnotationChips } from '@/features/annotations/annotation-chips'
import type { AnnotationResponse, MessageResponse } from '@/lib/api/types'

// An unmarked chip claims the model received that tag, so the marker set travels with the tags it
// judges: either both or neither, never tags on their own.
type TagProps =
  | { tags?: undefined; unsentTags?: undefined }
  | { tags: MessageResponse['tags']; unsentTags: Set<string> }

// Shared chat bubble for both the single-conversation view and the side-by-side panes.
// The optional flag/meta props are the superset the detail view uses; a pane just omits them.
export function Bubble({
  messageId,
  role,
  content,
  status,
  flagCount,
  extra,
  imageKeys,
  tags,
  unsentTags,
  tagContext,
  tagContextPartial,
  onFlag,
  annotations,
  onAnnotate,
}: {
  messageId?: string
  role: MessageResponse['role']
  content: string
  status?: MessageResponse['status']
  flagCount?: number
  extra?: MessageResponse['extra']
  imageKeys?: string[]
  tagContext?: Record<string, string> | null
  tagContextPartial?: boolean
  onFlag?: () => void
  // Every author's labels on this message — shared reads, unlike notes.
  annotations?: AnnotationResponse[]
  onAnnotate?: () => void
} & TagProps) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(content)
      } else {
        // Non-secure contexts (e.g. the console opened over a LAN IP rather than
        // localhost) expose no clipboard API — fall back to the legacy path.
        const ta = document.createElement('textarea')
        ta.value = content
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        ta.remove()
      }
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      toast.error('Could not copy to clipboard')
    }
  }
  if (role === 'system') {
    return (
      <div className="bg-muted text-muted-foreground mx-auto max-w-2xl rounded-md px-3 py-2 text-center text-xs">
        {content}
      </div>
    )
  }
  const isUser = role === 'user'
  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'group/bubble relative max-w-[80%] space-y-1 rounded-lg px-3 py-2 text-sm',
          isUser ? 'bg-primary text-primary-foreground' : 'bg-card border',
          !isUser && (flagCount ?? 0) > 0 && 'ring-warn/50 ring-1',
        )}
      >
        {imageKeys && imageKeys.length > 0 && <MessageAttachments imageKeys={imageKeys} />}
        {content ? <Markdown content={content} /> : <span>…</span>}
        {/* The tint follows the bubble's own colour, not the page surface: these sit on `bg-primary`
            or `bg-card`, where a `bg-muted` chip would clash in one of them. The row carries no
            `aria-label`, unlike the pane's: a transcript holds many bubbles, so one constant name
            would put N identical entries in a screen reader's element list — the list semantics and
            each chip's own marker carry it instead. */}
        {tags && Object.keys(tags).length > 0 && (
          <ul role="list" className="flex flex-wrap gap-1 pt-0.5" data-testid="message-tags">
            {Object.entries(tags).map(([k, v]) => {
              const unsent = unsentTags.has(k)
              // One label for the chip and its tooltip: a valueless tag reads as the bare key in both.
              const label = v ? `${k}: ${v}` : k
              return (
                <li key={k}>
                  <Badge
                    variant="tag"
                    className="max-w-[14rem] rounded-full border-current/20 bg-current/10 px-2 text-[10px] text-current"
                    title={unsent ? `${label} — not sent to the model` : label}
                  >
                    <span className="truncate">{label}</span>
                    {unsent && (
                      <>
                        <BanIcon className="ml-1 size-3 shrink-0" aria-hidden />
                        <span className="sr-only">(not sent to the model)</span>
                      </>
                    )}
                  </Badge>
                </li>
              )
            })}
          </ul>
        )}
        {annotations && <AnnotationChips annotations={annotations} />}
        {!isUser && (
          <TagContext tagContext={tagContext} partial={tagContextPartial} messageId={messageId} />
        )}
        {!isUser && <MessageMeta extra={extra} />}
        {((status && status !== 'complete') || (flagCount ?? 0) > 0) && (
          <div className="flex items-center gap-2 pt-0.5">
            {status && status !== 'complete' && (
              <span className="text-xs opacity-70">{status}</span>
            )}
            {(flagCount ?? 0) > 0 && (
              <span className="border-warn/40 bg-warn/10 text-warn inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium">
                <Flag className="size-3" /> {flagCount} flagged
              </span>
            )}
          </div>
        )}
        {/* Per-message actions: copy (always) + flag (assistant, when wired), revealed on hover/focus. */}
        <div className="absolute -top-2 right-2 flex gap-1 opacity-0 transition-opacity group-hover/bubble:opacity-100 focus-within:opacity-100">
          <button
            type="button"
            onClick={copy}
            title="Copy message"
            aria-label="Copy message"
            className="bg-card text-muted-foreground hover:text-foreground inline-flex size-6 items-center justify-center rounded-md border shadow-sm"
          >
            {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
          </button>
          {onFlag && (
            <button
              type="button"
              onClick={onFlag}
              title="Flag this reply for review"
              aria-label="Flag this reply for review"
              className="bg-card text-muted-foreground hover:text-foreground inline-flex size-6 items-center justify-center rounded-md border shadow-sm"
            >
              <Flag className="size-3" />
            </button>
          )}
          {onAnnotate && (
            <button
              type="button"
              onClick={onAnnotate}
              title="Label this message"
              aria-label="Label this message"
              className="bg-card text-muted-foreground hover:text-foreground inline-flex size-6 items-center justify-center rounded-md border shadow-sm"
            >
              <Tag className="size-3" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
