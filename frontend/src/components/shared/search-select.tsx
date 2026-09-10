import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Input } from '@/components/ui/input'
import { FormField } from '@/components/shared/form-field'
import { useAnchoredListbox } from '@/components/shared/use-anchored-listbox'
import { cn } from '@/lib/utils'

type Option = { value: string; label: string }

// Typeahead combobox: type and the list auto-opens + live-filters (debounced → server);
// click or ↑/↓+Enter to pick, Esc / outside-click to close. Shared by the export pickers.
// The listbox is positioned `fixed` (coords from the input's rect) so it escapes a dialog's
// inner `overflow-y-auto` clip while staying a descendant of the <dialog> top layer — a portal
// to document.body would render *under* a showModal() dialog instead.
// Contract note: `value` drives only `aria-selected` — the component does not render a label for a
// pre-seeded `value`. Both current call sites start empty; a pre-seeded reuse (edit form / deep
// link) needs a value→label resolution added first.
export function SearchSelect({
  label,
  htmlFor,
  value,
  onChange,
  onSearchChange,
  options,
  isPending,
  isError,
  error,
  emptyLabel = 'No matches.',
}: {
  label: string
  htmlFor: string
  value: string
  onChange: (value: string) => void
  onSearchChange: (term: string) => void
  options: Option[]
  isPending: boolean
  isError: boolean
  error?: string
  emptyLabel?: string
}) {
  const [term, setTerm] = useState('')
  const [active, setActive] = useState(-1)
  const { open, setOpen, rect, containerRef, listRef } = useAnchoredListbox()
  const skipSearch = useRef(false)
  const listId = useId()

  useEffect(() => {
    // A selection sets `term` to the label — don't re-search for it; only debounce real typing.
    if (skipSearch.current) {
      skipSearch.current = false
      return
    }
    const id = setTimeout(() => onSearchChange(term), 300)
    return () => clearTimeout(id)
  }, [term, onSearchChange])

  const select = (option: Option) => {
    onChange(option.value)
    skipSearch.current = true
    setTerm(option.label)
    setOpen(false)
    setActive(-1)
  }

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      // Esc closes only the dropdown — but inside a native <dialog> (showModal) an un-swallowed Esc
      // also fires the dialog's cancel, closing the whole modal (and wiping form state via onOpen).
      // Swallow it only while the list is open; a closed dropdown lets Esc reach the dialog normally.
      if (!open) return
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActive((i) => Math.min(i + 1, options.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true) // open so aria-activedescendant points at a rendered option, not a phantom id
      setActive((i) => Math.max(i - 1, 0))
    } else if (event.key === 'Enter' && open) {
      event.preventDefault() // an open combobox must not submit the surrounding form
      if (active >= 0 && options[active]) select(options[active])
    }
  }

  // Announce only the transient status (searching / error / empty) to screen readers via one dedicated
  // live region. The listbox itself carries no aria-live: navigation is announced by aria-activedescendant,
  // and a live listbox would re-announce the whole option set on every keystroke.
  const statusText = !open
    ? ''
    : isPending
      ? 'Searching…'
      : isError
        ? 'Could not load results.'
        : options.length === 0
          ? emptyLabel
          : ''

  return (
    <FormField label={label} htmlFor={htmlFor} error={error}>
      <span role="status" aria-live="polite" className="sr-only">
        {statusText}
      </span>
      <div ref={containerRef}>
        <Input
          id={htmlFor}
          role="combobox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-autocomplete="list"
          aria-activedescendant={active >= 0 && options[active] ? `${listId}-${active}` : undefined}
          placeholder="Search…"
          value={term}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            skipSearch.current = false // real typing always searches (clears any leftover skip)
            setTerm(event.target.value)
            setOpen(true)
            setActive(-1)
            if (event.target.value === '' && value) onChange('') // clearing the box clears the filter
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      {open && rect && (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          style={{ position: 'fixed', top: rect.top + 4, left: rect.left, width: rect.width }}
          className="bg-card z-50 max-h-56 overflow-auto rounded-md border py-1 text-sm shadow-md"
        >
          {isPending ? (
            // Status rows are role="presentation" (not selectable options) so the listbox holds only
            // options; they're purely visual — the sr-only live region above announces them.
            <li role="presentation" className="text-muted-foreground px-3 py-1.5">
              Searching…
            </li>
          ) : isError ? (
            <li role="presentation" className="text-destructive px-3 py-1.5">
              Could not load results.
            </li>
          ) : options.length === 0 ? (
            <li role="presentation" className="text-muted-foreground px-3 py-1.5">
              {emptyLabel}
            </li>
          ) : (
            options.map((option, i) => (
              <li
                key={option.value}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={option.value === value}
                className={cn(
                  'cursor-pointer px-3 py-1.5',
                  i === active ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
                )}
                onMouseDown={(event) => {
                  event.preventDefault() // fire before the input blur so the click isn't lost
                  select(option)
                }}
                onMouseEnter={() => setActive(i)}
              >
                {option.label}
              </li>
            ))
          )}
        </ul>
      )}
    </FormField>
  )
}
