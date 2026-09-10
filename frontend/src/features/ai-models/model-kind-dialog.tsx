import { ChevronRight } from 'lucide-react'
import { MODEL_KINDS, providerLabel, providersForKind, type ModelKind } from './labels'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'

const KINDS: ModelKind[] = ['provider', 'custom']

export function ModelKindDialog({
  open,
  onOpenChange,
  onChoose,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onChoose: (kind: ModelKind) => void
}) {
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      eyebrow="Inference"
      title="Add a model"
      className="max-w-2xl"
      dismissable
    >
      <p className="text-muted-foreground text-sm">How is this model reached?</p>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-3 sm:grid-cols-2">
        {KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => onChoose(kind)}
            // Without these the accessible name is the whole card — title, description and
            // provider list run together. `aria-labelledby` is what narrows it to the title;
            // `aria-describedby` alone would not, since the name falls back to contents.
            aria-labelledby={`kind-${kind}-title`}
            aria-describedby={`kind-${kind}-desc kind-${kind}-providers`}
            className="group bg-background hover:border-primary hover:bg-accent/40 focus-visible:ring-ring flex flex-col gap-2 rounded-lg border p-4 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            <span className="flex items-center justify-between gap-2">
              <span id={`kind-${kind}-title`} className="font-display text-sm font-semibold">
                {MODEL_KINDS[kind].title}
              </span>
              <ChevronRight
                className="text-muted-foreground group-hover:text-primary size-4 shrink-0 transition-transform group-hover:translate-x-0.5"
                aria-hidden
              />
            </span>
            {/* Grows so both cards' provider lists sit on the same baseline. */}
            <span
              id={`kind-${kind}-desc`}
              className="text-muted-foreground flex-1 text-xs leading-relaxed"
            >
              {MODEL_KINDS[kind].description}
            </span>
            {/* Wrapped as items rather than a joined string: a middot separator orphans
                at the start of the second line. */}
            <span
              id={`kind-${kind}-providers`}
              className="text-muted-foreground/70 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] tracking-[0.14em] uppercase"
            >
              {providersForKind(kind).map((p) => (
                <span key={p}>{providerLabel(p)}</span>
              ))}
            </span>
          </button>
        ))}
      </div>
      <div className="flex justify-end">
        <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
      </div>
    </Modal>
  )
}
