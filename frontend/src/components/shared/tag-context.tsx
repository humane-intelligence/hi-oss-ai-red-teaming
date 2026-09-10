import { useId } from 'react'
import { Badge } from '@/components/ui/badge'

// The tag context a reply was generated with. Already post-policy and post-sanitise, so unlike the
// live-tag chips there is nothing here to mark as "not sent".
export function TagContext({
  tagContext,
  partial,
  messageId,
}: {
  tagContext?: Record<string, string> | null
  // `tag_context_partial`: set when the map covers only a `continue`'s appended text, not the
  // whole reply (the superseded message it extended keeps its own record).
  partial?: boolean
  // Suffixes the test id, so a transcript rendering N records has N addressable blocks.
  messageId?: string
}) {
  // Sorted for a stable, readable order that doesn't depend on jsonb's storage order — not to
  // reproduce the prompt block's codepoint sort, which this can diverge from for non-ASCII keys.
  // Key order carries no meaning (see the API field's own description).
  const entries = Object.entries(tagContext ?? {}).sort(([a], [b]) => a.localeCompare(b))
  const labelId = useId()
  if (entries.length === 0) return null

  return (
    <div
      className="flex flex-wrap items-center gap-1 pt-1"
      data-testid={messageId ? `tag-context-${messageId}` : 'tag-context'}
    >
      {/* The label names the list: a message-tag list (tags as stored, possibly unsent) can sit
          directly above this one (tags proven sent), and unnamed they are indistinguishable in a
          screen reader's element list. */}
      <span
        id={labelId}
        className="text-muted-foreground font-mono text-[10px] tracking-wide uppercase"
      >
        {partial ? 'sent with the continuation only' : 'sent with'}
      </span>
      <ul role="list" aria-labelledby={labelId} className="flex flex-wrap gap-1">
        {entries.map(([key, value]) => {
          const label = `${key}: ${value}`
          return (
            <li key={key}>
              <Badge
                variant="tag"
                className="max-w-[14rem] rounded-full px-2 text-[10px]"
                title={label}
              >
                <span className="truncate">{label}</span>
              </Badge>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
