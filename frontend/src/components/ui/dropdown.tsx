import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

// Upstream's Button exports no props type of its own, so derive one.
type ButtonProps = ComponentProps<typeof Button>

// Minimal disclosure dropdown: a button toggles a floating panel that closes on
// outside-click or Escape. `children` is a render prop receiving `close` so panel
// actions can dismiss it. The trigger defaults to an outline button; pass
// `triggerVariant`/`triggerSize`/`triggerClassName` for a different one (e.g. a
// ghost icon with a badge overlay).
//
// Not superseded by `ui/dropdown-menu.tsx`: its three callers (notification bell, columns menu,
// saved views) hold checkbox lists and a notification list, so `role="menu"`/`menuitem` semantics
// would be a downgrade. `Popover` is what they would migrate to.
export function Dropdown({
  label,
  ariaLabel,
  align = 'end',
  disabled,
  panelClassName,
  triggerVariant = 'outline',
  triggerSize,
  triggerClassName,
  onOpenChange,
  children,
}: {
  label: ReactNode
  ariaLabel?: string
  align?: 'start' | 'end'
  disabled?: boolean
  panelClassName?: string
  triggerVariant?: ButtonProps['variant']
  triggerSize?: ButtonProps['size']
  triggerClassName?: string
  onOpenChange?: (open: boolean) => void
  children: (close: () => void) => ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const panelId = useId()

  // Nudge the panel back inside the viewport: anchored to the trigger's edge it can overhang the
  // window on a narrow screen — sideways, where there is no scroll to recover it. Written to the
  // node instead of to state so measuring and positioning stay one layout pass.
  useLayoutEffect(() => {
    const el = panelRef.current
    if (!open || !el) return
    const clamp = () => {
      el.style.transform = ''
      const rect = el.getBoundingClientRect()
      const margin = 8
      const shift =
        Math.max(0, margin - rect.left) - Math.max(0, rect.right - (window.innerWidth - margin))
      if (shift) el.style.transform = `translateX(${shift}px)`
    }
    clamp()
    window.addEventListener('resize', clamp)
    // The panel is content-sized, and `onOpenChange` exists so a parent can defer a fetch until it
    // opens — so content arriving (or changing) after the first measurement is the designed-for
    // case, and a measure-once clamp would keep a stale shift. Observe the panel itself, not the
    // window. No scroll listener: the panel is positioned relative to the trigger, so it travels
    // with it.
    const observer = new ResizeObserver(clamp)
    observer.observe(el)
    return () => {
      window.removeEventListener('resize', clamp)
      observer.disconnect()
    }
  }, [open])

  // Notify the parent on real open/close transitions (all paths: toggle, outside-click, Escape,
  // focus-out) so it can, e.g., defer a fetch until the panel opens. Via a ref so the callback
  // needn't be referentially stable, and skipping the mount so a fresh dropdown doesn't fire a
  // spurious `close`.
  const onOpenChangeRef = useRef(onOpenChange)
  useEffect(() => {
    onOpenChangeRef.current = onOpenChange
  }, [onOpenChange])
  const didMount = useRef(false)
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true
      return
    }
    onOpenChangeRef.current?.(open)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      // Escape returns focus to the trigger so it never lands on <body>;
      // outside-click closes without stealing focus back.
      if (e.key === 'Escape') {
        setOpen(false)
        ref.current?.querySelector<HTMLButtonElement>(':scope > button')?.focus()
      }
    }
    const onFocusOut = (e: FocusEvent) => {
      // Tabbing out of the panel (or into a sibling trigger) must dismiss it —
      // mousedown alone leaves the panel orphaned for keyboard users. Ignore a
      // null relatedTarget: focus dropping to <body> is what Safari/Firefox emit
      // when a label click doesn't focus the checkbox — closing there would kill
      // the toggle before it applies. Genuine tab-out always has a real target.
      const next = e.relatedTarget as Node | null
      if (next && ref.current && !ref.current.contains(next)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('focusout', onFocusOut)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('focusout', onFocusOut)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <Button
        type="button"
        variant={triggerVariant}
        size={triggerSize}
        className={triggerClassName}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
      >
        {label}
      </Button>
      {open && (
        // Click-focusable so a press on a non-focusable row (a <label>) lands focus here rather
        // than on the nearest focusable ancestor outside — which `onFocusOut` would read as a
        // tab-out and close mid-press, killing the click.
        <div
          id={panelId}
          ref={panelRef}
          tabIndex={-1}
          className={cn(
            'bg-card text-card-foreground absolute z-20 mt-1 max-w-[calc(100vw-1rem)] min-w-52 rounded-md border p-1 shadow-lg',
            align === 'end' ? 'right-0' : 'left-0',
            panelClassName,
          )}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  )
}
