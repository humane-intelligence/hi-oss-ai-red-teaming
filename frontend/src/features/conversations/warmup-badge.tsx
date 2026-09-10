import { Check, Loader2, RotateCcw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { WarmupState } from './use-model-warmup'

/**
 * Presentational readiness pill, driven by `useWarmupGate` state in the parent
 * (which owns the probe loop so the composer can also block on it).
 *
 * Renders nothing until the first probe is in flight, then "Waking model…" while a
 * cold endpoint boots, a green "Model ready" on success, and a retry on failure.
 */
export function WarmupBadge({ state, onRetry }: { state: WarmupState; onRetry: () => void }) {
  if (state === 'idle') return null
  if (state === 'ready') {
    return (
      <Badge variant="ok" className="gap-1">
        <Check className="size-3" /> Model ready
      </Badge>
    )
  }
  if (state === 'starting') {
    return (
      <Badge variant="warn" className="gap-1">
        <Loader2 className="size-3 animate-spin" /> Waking model…
      </Badge>
    )
  }
  // error | timeout — offer a manual retry.
  return (
    <button type="button" onClick={onRetry} title="Try waking the model again">
      <Badge variant="err" className="gap-1">
        <RotateCcw className="size-3" /> Model unavailable — retry
      </Badge>
    </button>
  )
}
