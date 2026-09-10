import { useEffect, useLayoutEffect, useRef, useState } from 'react'

type Rect = { top: number; left: number; width: number }

/**
 * Shared fixed-position listbox anchoring for typeahead comboboxes. The listbox is positioned
 * `fixed` (coords from the input's rect) so it escapes a dialog's inner `overflow-y-auto` clip
 * while staying a descendant of the <dialog> top layer — a portal to document.body would render
 * *under* a showModal() dialog. Keeps the listbox anchored on scroll/resize (scroll uses capture
 * so a scroll in the dialog's own overflow container repositions it too, not just the window),
 * and closes on an outside click.
 */
export function useAnchoredListbox() {
  const [open, setOpen] = useState(false)
  const [rect, setRect] = useState<Rect | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const el = containerRef.current
      if (el) {
        const r = el.getBoundingClientRect()
        setRect({ top: r.bottom, left: r.left, width: r.width })
      }
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (!containerRef.current?.contains(target) && !listRef.current?.contains(target))
        setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return { open, setOpen, rect, containerRef, listRef }
}
