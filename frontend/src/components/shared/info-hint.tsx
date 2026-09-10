import { useId, type CSSProperties } from 'react'
import { Info } from 'lucide-react'

// A hover / keyboard / touch explanation next to a label.
//
// By default `aria-label` carries the explanation itself (not just "info"), so screen-reader users
// get the help text from the name alone. `title` alone would not reach everyone else: it needs a
// hover, which a touch device never produces, and a non-focusable span cannot be reached by keyboard
// — so the text also opens in a native popover from a real button. The popover is the browser's own
// (no dependency, top layer, Escape and light-dismiss for free), matching the repo's native-`<dialog>`
// idiom.
//
// `label` opts out of that when `text` is not name-shaped: a hint quoting machine output (a provider
// error, a stack) would announce a JSON blob as the button's name. The short name then says an
// explanation exists and `aria-describedby` points at the panel for the text itself — a node
// referenced directly by that attribute is included in the description computation even while the
// popover is closed, so this does not depend on `title`, which AT expose inconsistently (iOS
// VoiceOver does not). The relation is wired only with `label`. Without it the name already carries
// the text and `title` describes the button with the same string anyway — a duplicate accepted to
// keep the hover tooltip for the label-less call sites.
//
// Deliberately not `use-anchored-listbox` (the JS rect + `fixed` idiom the comboboxes use): the top
// layer escapes a dialog's `overflow-y-auto` without that hook's portal-under-`showModal` problem,
// and Esc plus light-dismiss come for free. Two idioms on purpose; migrating the listbox is its own
// change.
//
// A popover knows nothing about the button that opened it: the UA default is `inset: 0; margin: auto`,
// which centres it in the viewport. Anchoring it to the trigger needs CSS anchor positioning, hence
// the per-instance `anchor-name`. Where that is unsupported the panel lands at the viewport's
// top-left rather than centred — the classes below drop the UA `margin: auto`, over-constraining
// `inset: 0`. Degradation, not break. Read off the cascade, not measured.
export function InfoHint({ text, label }: { text: string; label?: string }) {
  const id = useId()
  // `useId` returns a colon-wrapped value; an anchor name has to be a plain dashed ident.
  const anchor = `--hint-${id.replace(/[^a-zA-Z0-9]/g, '')}`
  const anchorVar = { '--hint-anchor': anchor } as CSSProperties
  return (
    <>
      <button
        type="button"
        popoverTarget={id}
        aria-label={label ?? text}
        aria-describedby={label ? id : undefined}
        title={text}
        style={anchorVar}
        // size-6 is the 24px minimum tap target; the icon stays small inside it.
        className="text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-full [anchor-name:var(--hint-anchor)] focus-visible:ring-2 focus-visible:outline-none"
      >
        <Info className="size-3.5" aria-hidden />
      </button>
      <div
        id={id}
        popover="auto"
        // Focusable because it scrolls: opening a popover via `popovertarget` does not move focus,
        // so a clipped panel would be unreachable by keyboard (WCAG 2.1.1).
        tabIndex={0}
        style={anchorVar}
        // `span-all` on the inline axis, so the width comes from the max-width clamp rather than
        // from how much room happens to sit between the trigger and the edge — anchoring to the
        // trigger's inline-start squeezed the panel to ~175px next to a right-hand aside.
        // Symmetric: `flip-block` moves the panel, not the margin, so a top-only gap dies on flip.
        className="bg-card text-foreground border-border m-0 my-1 max-h-[min(24rem,60vh)] max-w-[min(20rem,calc(100vw-2rem))] overflow-y-auto rounded-md border p-3 text-sm break-words shadow-lg [position-anchor:var(--hint-anchor)] [position-area:block-end_span-all] [position-try-fallbacks:flip-block]"
      >
        {text}
      </div>
    </>
  )
}
