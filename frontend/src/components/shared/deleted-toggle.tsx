import { useId } from 'react'
import { InfoHint } from '@/components/shared/info-hint'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

type DeletedToggleProps = {
  value: boolean
  onChange: (next: boolean) => void
  label: string
}

// Switches a list between its live rows and the recently-deleted ones. A select
// rather than a checkbox so it reads as "which set am I looking at" and sits in
// the same control row as the other list filters.
export function DeletedToggle({ value, onChange, label }: DeletedToggleProps) {
  const labelId = useId()
  const triggerId = useId()
  return (
    <div className="flex min-w-0 flex-1 basis-[45%] items-center gap-1.5 sm:flex-none sm:basis-auto">
      <span id={labelId} className="sr-only">
        {label}
      </span>
      <Select value={value ? 'deleted' : 'live'} onValueChange={(v) => onChange(v === 'deleted')}>
        {/* Both ids: `aria-label` alone would replace the trigger's content as the accessible
            name, so the chosen set stopped being announced. Naming the label *and* the trigger
            concatenates them, which is what the native `<select aria-label>` gave for free. */}
        <SelectTrigger
          id={triggerId}
          aria-labelledby={`${labelId} ${triggerId}`}
          className="min-w-0 flex-1 sm:w-44 sm:flex-none"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value="live">Active</SelectItem>
            <SelectItem value="deleted">Recently deleted</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
      {/* Was a `title`, which a phone cannot long-press to and a keyboard cannot reach at all -
          the same objection `page-actions.tsx` records for its inline buttons. */}
      <InfoHint text="Deleted items stay restorable for a limited time." />
    </div>
  )
}
