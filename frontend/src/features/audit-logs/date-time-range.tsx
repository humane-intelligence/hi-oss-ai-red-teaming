import { cn } from '@/lib/utils'
// One bordered, full-width control per bound, with the label inside it. The two native pickers
// carry their own irreducible widths, so a bare row of them either clipped the time or floated the
// date's calendar icon; grouping them under one border lets the pair fill the row instead.
//
// Raw `<input>`s rather than the `Input` primitive on purpose: the border, background, radius,
// padding and focus ring all belong to the group here, so `Input` would have to be unstyled back
// down to a bare element to fit.
export function DateTimeRange({
  label,
  dateName,
  dateAriaLabel,
  dateValue,
  onDateChange,
  timeName,
  timeAriaLabel,
  timeValue,
  onTimeChange,
}: {
  label: string
  dateName: string
  dateAriaLabel: string
  dateValue: string
  onDateChange: (next: string) => void
  timeName: string
  timeAriaLabel: string
  timeValue: string
  onTimeChange: (next: string) => void
}) {
  const inner =
    'h-9 min-w-0 border-0 bg-transparent p-0 text-sm outline-none focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-50'
  return (
    <div className="bg-card focus-within:border-ring focus-within:ring-ring/50 flex w-full items-center gap-2 rounded-md border px-3 shadow-xs focus-within:ring-[3px] sm:w-auto">
      <span className="text-muted-foreground shrink-0 text-sm">{label}</span>
      <input
        type="date"
        name={dateName}
        aria-label={dateAriaLabel}
        value={dateValue}
        onChange={(e) => onDateChange(e.target.value)}
        className={cn(inner, 'flex-1')}
      />
      <input
        type="time"
        name={timeName}
        aria-label={timeAriaLabel}
        // inert until the date is picked: its value only feeds the range bound then
        disabled={!dateValue}
        value={timeValue}
        onChange={(e) => onTimeChange(e.target.value)}
        className={cn(inner, 'w-[6.5rem] shrink-0 text-right')}
      />
    </div>
  )
}
