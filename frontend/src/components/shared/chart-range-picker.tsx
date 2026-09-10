import { useRef, type KeyboardEvent } from 'react'
import { cn } from '@/lib/utils'
import type { ChartRange, ChartRangeOption } from './chart-range-ranges'

export function ChartRangePicker({
  value,
  options,
  onChange,
}: {
  value: ChartRange
  // Owned by the chart, which knows how long the series is and drops the presets that would window
  // nothing.
  options: ChartRangeOption[]
  onChange: (next: ChartRange) => void
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  // A radio group, not a tablist: selection follows focus, so arrows pick as they move.
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = options.findIndex((option) => option.value === value)
    const target = {
      ArrowRight: (current + 1) % options.length,
      ArrowDown: (current + 1) % options.length,
      ArrowLeft: (current - 1 + options.length) % options.length,
      ArrowUp: (current - 1 + options.length) % options.length,
      Home: 0,
      End: options.length - 1,
    }[event.key]
    if (target === undefined) return
    event.preventDefault()
    onChange(options[target]!.value)
    refs.current[target]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label="Time range"
      aria-orientation="horizontal"
      onKeyDown={onKeyDown}
      className="flex gap-1"
    >
      {options.map((option, i) => {
        const checked = option.value === value
        return (
          <button
            key={option.value}
            ref={(node) => {
              refs.current[i] = node
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(option.value)}
            className={cn(
              // `h-8` matches the design system's small button: at 24 px this was the only tap
              // target on the page below the app's own floor (buttons 32, tabs 38).
              'h-8 rounded px-3 text-xs transition-colors',
              checked
                ? 'bg-muted text-foreground font-medium'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
