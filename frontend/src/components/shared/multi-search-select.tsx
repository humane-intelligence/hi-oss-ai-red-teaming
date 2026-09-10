import { useEffect, useId, useState, type KeyboardEvent } from 'react'
import { Check, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { FormField } from '@/components/shared/form-field'
import { useAnchoredListbox } from '@/components/shared/use-anchored-listbox'
import { cn } from '@/lib/utils'

// `hint` is per-option side information (a count, a status) shown right-aligned in the list only —
// never in the chip, which names the picked value. `hintTitle` is for a caller whose hint is a
// summary: the hint is what gets read out and truncated, this is what hover reveals.
type Option = { value: string; label: string; hint?: string; hintTitle?: string }

// Multi-select sibling of `SearchSelect` (shares the fixed-listbox anchoring hook). Unlike the
// single-select, a pick toggles membership and keeps the list open; the search box is persistent
// (never overwritten by a pick); selected values render as removable chips above the input. Server
// search narrows `options`, so labels of already-picked values are cached to keep chips labelled.
export function MultiSearchSelect({
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
  placeholder = 'Search…',
  allowCreate = false,
  describedBy,
}: {
  label: string
  htmlFor: string
  value: string[]
  onChange: (next: string[]) => void
  onSearchChange: (term: string) => void
  options: Option[]
  isPending: boolean
  isError: boolean
  error?: string
  emptyLabel?: string
  placeholder?: string
  // Opt-in: the typed term becomes a pickable value of its own, for a field whose options are a
  // suggestion set rather than a closed list (model labels). Off, so the pickers over real entities
  // (reviewers, models) can't invent an id.
  allowCreate?: boolean
  // Id of prose explaining the field — the create-a-new-value affordance is not discoverable from
  // the combobox role alone, so the caller that enables `allowCreate` needs somewhere to say it.
  describedBy?: string
}) {
  const [term, setTerm] = useState('')
  // A picked chip is silent otherwise: with `allowCreate` the created value is in neither the
  // options nor the create row afterwards, so the only feedback is the input clearing.
  const [announcement, setAnnouncement] = useState('')
  const [active, setActive] = useState(-1)
  const { open, setOpen, rect, containerRef, listRef } = useAnchoredListbox()
  const listId = useId()

  useEffect(() => {
    const id = setTimeout(() => onSearchChange(term), 300)
    return () => clearTimeout(id)
  }, [term, onSearchChange])

  // A picked value may scroll out of the server-narrowed option set; accumulate labels so its chip
  // stays readable. Derived state updated during render (React's sanctioned "store info from previous
  // renders" pattern, guarded so it converges) — not a ref read in render, not setState in an effect.
  const [seenLabels, setSeenLabels] = useState<Record<string, string>>({})
  let labelMap = seenLabels
  const pending: Record<string, string> = {}
  for (const o of options) if (labelMap[o.value] !== o.label) pending[o.value] = o.label
  if (Object.keys(pending).length > 0) {
    labelMap = { ...labelMap, ...pending }
    setSeenLabels(labelMap)
  }
  const labelFor = (v: string) => labelMap[v] ?? v

  const toggle = (v: string) => {
    setAnnouncement(`${value.includes(v) ? 'Removed' : 'Added'} ${labelFor(v)}`)
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v])
    // Deliberately keep the list open — multi-select expects several picks in a row.
  }

  // The create row is prepended to the *rendered* rows only; `labelMap` keeps accumulating from the
  // `options` prop alone, so a created value's chip reads the value itself, not "Create …".
  const trimmed = term.trim()
  const holds = (candidates: string[]) =>
    candidates.some((c) => c.toLowerCase() === trimmed.toLowerCase())
  // Values *and* labels: a caller whose values are opaque ids (annotation labels) would
  // otherwise be offered `Create "Jailbreak"` for a label the list already holds, one
  // ArrowDown+Enter from an ad-hoc twin of a catalog entry. Callers whose value equals its
  // label (model labels) are unaffected — the two checks coincide.
  const optionLabels = options.map((o) => o.label)
  const createRow: Option[] =
    allowCreate &&
    trimmed !== '' &&
    !holds(options.map((o) => o.value)) &&
    !holds(optionLabels) &&
    !holds(value)
      ? [{ value: trimmed, label: `Create "${trimmed}"` }]
      : []
  const rows = [...createRow, ...options]
  // Without this the operator who re-types a label they already picked is told "no labels in
  // use yet" — the create row is suppressed precisely because they *do* have it.
  const noRowsLabel =
    allowCreate && trimmed !== '' && (holds(value) || holds(value.map(labelFor)))
      ? 'Already added.'
      : emptyLabel

  const pick = (index: number) => {
    const row = rows[index]
    if (!row) return
    toggle(row.value)
    // Clearing only after a create, so typing several new values in a row works; a pick from the
    // list leaves the search box alone, as the persistent-search contract says.
    if (createRow.length > 0 && index === 0) {
      setTerm('')
      setActive(-1)
    }
  }

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      // Swallow only while open so a surrounding <dialog> isn't cancelled mid-pick (see SearchSelect).
      if (!open) return
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActive((i) => Math.min(i + 1, rows.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true) // open so aria-activedescendant points at a rendered option, not a phantom id
      setActive((i) => Math.max(i - 1, 0))
    } else if (event.key === 'Enter' && open) {
      event.preventDefault() // an open combobox must not submit the surrounding form
      if (active >= 0) pick(active)
    }
  }

  // Announce only the transient status to screen readers via one live region (see SearchSelect).
  const statusText = !open
    ? ''
    : isPending
      ? 'Searching…'
      : isError
        ? 'Could not load results.'
        : rows.length === 0
          ? noRowsLabel
          : ''

  return (
    <FormField label={label} htmlFor={htmlFor} error={error}>
      {/* Two regions, not one holding whichever message won: a chip announcement is cleared only by
          the next keystroke, so letting it mask the status latched the region — and the create flow
          refetches the options right after a pick, exactly when the status has something to say.
          Joining them instead would re-read the chip on every status flip, these being atomic. */}
      <span
        role="status"
        aria-live="polite"
        data-testid="multi-select-announcement"
        className="sr-only"
      >
        {announcement}
      </span>
      <span role="status" aria-live="polite" data-testid="multi-select-status" className="sr-only">
        {statusText}
      </span>
      {value.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1">
          {value.map((v) => (
            <span
              key={v}
              className="bg-muted inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs"
            >
              {labelFor(v)}
              <button
                type="button"
                aria-label={`Remove ${labelFor(v)}`}
                onClick={() => {
                  // Removing the last chip unmounts the chip row; keep focus on the input rather than
                  // letting it fall to <body> (the input lives inside containerRef).
                  const wasLast = value.length === 1
                  toggle(v)
                  if (wasLast) containerRef.current?.querySelector('input')?.focus()
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <div ref={containerRef}>
        <Input
          id={htmlFor}
          role="combobox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-autocomplete="list"
          aria-activedescendant={active >= 0 && rows[active] ? `${listId}-${active}` : undefined}
          aria-describedby={describedBy}
          placeholder={placeholder}
          value={term}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setTerm(event.target.value)
            setAnnouncement('')
            setOpen(true)
            setActive(-1)
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      {open && rect && (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          aria-multiselectable="true"
          style={{ position: 'fixed', top: rect.top + 4, left: rect.left, width: rect.width }}
          className="bg-card z-50 max-h-56 overflow-auto rounded-md border py-1 text-sm shadow-md"
        >
          {/* Rows come first and the status lines only join them, rather than replacing them:
              creating a value needs no options at all, so a failed (or in-flight) option query must
              not take the create row down with it — that is the whole affordance for a caller whose
              options are a suggestion set. Status rows are purely visual (the sr-only live region
              above announces them) and stay out of `rows`, so the option indices keep matching. */}
          {rows.map((option, i) => {
            const selected = value.includes(option.value)
            return (
              <li
                key={option.value}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={selected}
                className={cn(
                  'flex cursor-pointer items-center gap-2 px-3 py-1.5',
                  i === active ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
                )}
                onMouseDown={(event) => {
                  event.preventDefault() // fire before the input blur so the click isn't lost
                  pick(i)
                }}
                onMouseEnter={() => setActive(i)}
              >
                <Check
                  className={cn('size-3.5 shrink-0', selected ? 'opacity-100' : 'opacity-0')}
                />
                {/* Truncated, so the full value has to stay reachable: the other caller of this
                      component picks AI models, whose names differ near the end. */}
                <span className="min-w-0 truncate" title={option.label}>
                  {option.label}
                </span>
                {option.hint && (
                  <>
                    {/* A real space: the accessible name concatenates these nodes, and without it
                          a reader hears "ada@example.com2 active reviews". */}{' '}
                    {/* Capped: the label truncates, so an uncapped hint would win the row and
                          collapse the very name the `title` above exists to keep readable. */}
                    <span
                      className="text-muted-foreground ml-auto max-w-[45%] shrink-0 truncate text-xs"
                      title={option.hintTitle ?? option.hint}
                    >
                      {option.hint}
                    </span>
                  </>
                )}
              </li>
            )
          })}
          {isPending && (
            <li role="presentation" className="text-muted-foreground px-3 py-1.5">
              Searching…
            </li>
          )}
          {isError && (
            <li role="presentation" className="text-destructive px-3 py-1.5">
              Could not load results.
            </li>
          )}
          {!isPending && !isError && rows.length === 0 && (
            <li role="presentation" className="text-muted-foreground px-3 py-1.5">
              {noRowsLabel}
            </li>
          )}
        </ul>
      )}
    </FormField>
  )
}
