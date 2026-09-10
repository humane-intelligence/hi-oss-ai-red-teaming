import { Markdown } from '@/components/shared/markdown'

// A named, focusable region: a scroll container is no tab stop on its own, and neither the gate's
// buttons nor a dialog's other stops scroll it — so without this a keyboard-only reader gets the
// first screenful of a document that can run to tens of KB. Same call as `license-text-dialog`.
export function TermsText({ content, label }: { content: string; label: string }) {
  return (
    <div
      tabIndex={0}
      role="region"
      aria-label={label}
      className="bg-muted max-h-[24rem] overflow-auto rounded-md p-4"
    >
      <Markdown content={content} />
    </div>
  )
}
