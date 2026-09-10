import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type DrawerProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  // Names the dialog for assistive tech; the panel itself carries no visible heading.
  label: string
  className?: string
  children: ReactNode
}

// Left-anchored slide-in panel on a native <dialog>: showModal() supplies the focus trap, Escape,
// the inert background and focus return to the trigger. The close button floats over the scrolling
// content so it stays reachable while the nav scrolls under it.
export function Drawer({ open, onOpenChange, label, className, children }: DrawerProps) {
  const ref = useRef<HTMLDialogElement>(null)
  const pressedBackdrop = useRef(false)

  // Layout effect, not passive: the children unmount in the same commit that flips `open`, so
  // closing after paint could show the panel for a frame with nothing in it.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) el.showModal()
    else if (!open && el.open) el.close()
  }, [open])

  // showModal() makes the page behind inert but leaves it scrollable, which drags the panel's
  // backdrop over unrelated content on a phone.
  useLayoutEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  return (
    <dialog
      ref={ref}
      aria-label={label}
      onClose={() => onOpenChange(false)}
      onPointerDown={(e) => {
        // Same rule as modal.tsx: a click is only a backdrop dismiss when the press landed on the
        // <dialog> itself, so dragging out of the panel and releasing on the backdrop doesn't close.
        pressedBackdrop.current = e.target === ref.current
      }}
      onClick={(e) => {
        const released = pressedBackdrop.current
        pressedBackdrop.current = false
        if (e.target === ref.current && released) onOpenChange(false)
      }}
      className={cn(
        'border-sidebar-border bg-sidebar text-sidebar-foreground open:animate-in open:slide-in-from-left m-0 h-svh max-h-svh w-72 max-w-none flex-col border-r-2 p-0 shadow-lg backdrop:bg-black/50 open:flex open:duration-300 dark:backdrop:bg-black/70',
        className,
      )}
    >
      {open && (
        <>
          <div className="flex-1 overflow-y-auto p-3">{children}</div>
          <Button
            variant="ghost"
            size="icon"
            className="absolute top-2 right-2"
            onClick={() => onOpenChange(false)}
          >
            <X className="size-4" />
            <span className="sr-only">Close</span>
          </Button>
        </>
      )}
    </dialog>
  )
}
