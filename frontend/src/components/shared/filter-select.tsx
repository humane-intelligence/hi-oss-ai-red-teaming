import {
  EMPTY_VALUE,
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useId } from 'react'

export type FilterOption = { value: string; label: string }

export function FilterSelect({
  id,
  label,
  value,
  onChange,
  allLabel,
  options,
  className,
}: {
  id?: string
  label: string
  value: string
  onChange: (next: string) => void
  allLabel: string
  options: FilterOption[]
  className?: string
}) {
  const labelId = useId()
  const generatedTriggerId = useId()
  const triggerId = id ?? generatedTriggerId
  return (
    <>
      <span id={labelId} className="sr-only">
        {label}
      </span>
      <Select
        value={value === '' ? EMPTY_VALUE : value}
        onValueChange={(v) => onChange(v === EMPTY_VALUE ? '' : v)}
      >
        {/* Both ids: `aria-label` alone would replace the trigger's content as the accessible name,
          so the selected filter stopped being announced. Naming the label *and* the trigger
          concatenates them, which is what the native `<select aria-label>` gave for free. */}
        <SelectTrigger
          id={triggerId}
          aria-labelledby={`${labelId} ${triggerId}`}
          // Merged, not defaulted: an override that replaced this would drop `min-w-0`, and a
          // select's intrinsic minimum is its widest option, which `w-full` cannot undo.
          //
          // `w-[45%] grow`, not `flex-1 basis-[45%]`: both give a base size of 45% in the wrapping
          // filter toolbars, so two filters share a phone row, but `width` is the inline axis by
          // definition. `flex-basis` is the *main* axis, which is the block axis in the `flex-col`
          // `Field` these also sit inside - a percentage there means 45% of the height.
          //
          // Inside a `Field` the width is decided by its `[&>*]:w-full` regardless: a compound
          // selector, so it ties on specificity and wins on source order.
          className={cn('w-[45%] min-w-0 grow sm:w-36 sm:flex-none', className)}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value={EMPTY_VALUE}>{allLabel}</SelectItem>
            {options.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </>
  )
}
