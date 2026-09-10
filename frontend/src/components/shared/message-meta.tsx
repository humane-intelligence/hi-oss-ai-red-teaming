// Per-message generation telemetry from a message's `extra` (finish_reason,
// token usage, error). Loosely typed on the wire, so read defensively.
export function MessageMeta({ extra }: { extra?: Record<string, unknown> | null }) {
  if (!extra) return null

  const finish = typeof extra.finish_reason === 'string' ? extra.finish_reason : null
  const usage =
    extra.usage && typeof extra.usage === 'object' ? (extra.usage as Record<string, unknown>) : null
  const total = usage && typeof usage.total_tokens === 'number' ? usage.total_tokens : null
  const error =
    typeof extra.error === 'string'
      ? extra.error
      : extra.error && typeof extra.error === 'object'
        ? 'error'
        : null

  const parts: string[] = []
  if (finish) parts.push(`finish: ${finish}`)
  if (total != null) parts.push(`${total} tok`)
  if (parts.length === 0 && !error) return null

  return (
    <div className="text-muted-foreground flex flex-wrap items-center gap-2 pt-0.5 font-mono text-[10px] tracking-wide">
      {parts.length > 0 && <span className="uppercase">{parts.join(' · ')}</span>}
      {error && <span className="text-destructive">⚠ {error}</span>}
    </div>
  )
}
