import { RotateCcw, StepForward } from 'lucide-react'
import { Button } from '@/components/ui/button'

// The regenerate/continue row under the last assistant reply — shared by the single
// conversation and the side-by-side panes so both offer the same follow-up actions.
export function AssistantActions({
  onRegenerate,
  onContinue,
  disabled,
}: {
  onRegenerate: () => void
  onContinue: () => void
  disabled?: boolean
}) {
  return (
    <div className="mb-2 flex justify-end gap-1">
      <Button
        variant="ghost"
        size="sm"
        disabled={disabled}
        title="Redraw the last reply from scratch"
        onClick={onRegenerate}
      >
        <RotateCcw className="size-3.5" /> Regenerate
      </Button>
      <Button
        variant="ghost"
        size="sm"
        disabled={disabled}
        title="Let the model keep writing from where it stopped"
        onClick={onContinue}
      >
        <StepForward className="size-3.5" /> Continue
      </Button>
    </div>
  )
}
