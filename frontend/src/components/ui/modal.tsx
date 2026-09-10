import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { cn } from '@/lib/utils'

// `showModal()` makes everything outside the dialog inert, so a primitive that portals to
// `document.body` renders but cannot be clicked. Portalled primitives read the element from here
// instead of hunting for `dialog[open]`, which picks the first open dialog rather than the enclosing
// one and is only populated after the open effect has run.
//
// Ref callbacks fire in the commit phase, so the first render and commit still see `null` and
// children move to the dialog on the follow-up render. That is fine for a listbox, which mounts on
// open long after; a primitive that must portal correctly on its very first render would need the
// element sooner than this can give it.
const ModalDialogContext = createContext<HTMLDialogElement | null>(null)

export const useModalDialog = () => useContext(ModalDialogContext)

type ModalProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  eyebrow?: string
  className?: string
  // Runs once each time the modal opens, before it shows — use to reset form state.
  onOpen?: () => void
  // Opt in to backdrop click-to-close (light-dismiss). Off by default so form dialogs and
  // destructive confirms (unsaved input) aren't dismissed by a stray click — those keep Esc + an
  // explicit Cancel. Enable only for low-stakes dialogs where a re-open is cheap.
  dismissable?: boolean
  // Hold the dialog open while a write is in flight: Esc bypasses a disabled Cancel button, and a
  // dialog that unmounts mid-write takes its own error surface with it.
  busy?: boolean
  children: ReactNode
}

export function Modal({
  open,
  onOpenChange,
  title,
  eyebrow,
  className,
  onOpen,
  dismissable = false,
  busy = false,
  children,
}: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null)
  // State as well as the ref: children need a re-render once the element exists, or the context
  // would stay empty for their whole life.
  const [dialogEl, setDialogEl] = useState<HTMLDialogElement | null>(null)
  // Stable, so React does not detach and reattach it on every commit.
  const attach = useCallback((el: HTMLDialogElement | null) => {
    ref.current = el
    setDialogEl(el)
  }, [])
  // A click that only *releases* on the backdrop isn't a dismiss — a drag/select starting inside
  // content and ending on the backdrop reports the <dialog> as the click target too. Require the
  // press to also start on the backdrop, so in-progress input is never discarded by such a drag.
  const pressedBackdrop = useRef(false)
  const titleId = useId()

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) {
      onOpen?.()
      el.showModal()
    } else if (!open && el.open) {
      el.close()
    }
  }, [open, onOpen])

  useEffect(() => {
    const el = ref.current
    if (!el || !busy) return
    // A native listener on the dialog, not React's `onKeyDown`: React registers `keydown` on the root
    // container, so a child that stops propagation keeps the handler from ever running. Preventing the
    // key event is what holds the dialog — the close request is driven by the keypress, and Chrome then
    // fires neither `cancel` nor `close`. Subscribed only while busy, so nothing has to be mirrored.
    const holdEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') e.preventDefault()
    }
    el.addEventListener('keydown', holdEscape, true)
    return () => el.removeEventListener('keydown', holdEscape, true)
  }, [busy])

  return (
    <dialog
      ref={attach}
      aria-labelledby={titleId}
      onClose={() => onOpenChange(false)}
      onCancel={(e) => {
        // Esc is handled by the listener above; this covers a close request that doesn't come from the
        // keyboard (`requestClose()`, or a platform close signal).
        if (busy) e.preventDefault()
      }}
      onPointerDown={(e) => {
        // Record whether the press landed on the backdrop: target is the <dialog> element itself
        // (not a content child) and the point is outside the card's box (not its padding).
        const el = ref.current
        if (!el || e.target !== el) {
          pressedBackdrop.current = false
          return
        }
        const r = el.getBoundingClientRect()
        pressedBackdrop.current =
          e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom
      }}
      onClick={(e) => {
        // Light-dismiss (opt-in): close only when BOTH the press and the release were on the
        // backdrop. The target check also ignores keyboard-synthesized clicks (target is the
        // focused child, not the dialog); the press check ignores drag-outs from content.
        if (!dismissable || busy) return
        const el = ref.current
        const released = pressedBackdrop.current
        pressedBackdrop.current = false
        if (!el || e.target !== el || !released) return
        onOpenChange(false)
      }}
      className={cn(
        'bg-card text-card-foreground m-auto w-full max-w-md rounded-xl border p-6 shadow-lg backdrop:bg-black/50',
        className,
      )}
    >
      {open && (
        <ModalDialogContext.Provider value={dialogEl}>
          <div className="space-y-4">
            <header className="space-y-1">
              {eyebrow && (
                <p className="text-muted-foreground/70 font-mono text-[10px] tracking-[0.2em] uppercase">
                  {eyebrow}
                </p>
              )}
              <h2 id={titleId} className="font-display text-lg font-semibold tracking-tight">
                {title}
              </h2>
            </header>
            {children}
          </div>
        </ModalDialogContext.Provider>
      )}
    </dialog>
  )
}
