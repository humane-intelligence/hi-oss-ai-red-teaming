import { Input } from '@/components/ui/input'
import {
  EMPTY_VALUE,
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

/** Whether `allowedKeys` reflects a settled allow-list query. Unrestricted evaluations are `success`
 *  — there is no query to wait for. Required, not defaulted: defaulting to "settled" is the unsafe
 *  direction for the value that stops a pending query reading as "nothing allowed". */
export type AllowedKeysStatus = 'pending' | 'error' | 'success'

// The key half of a tag row, shared by the conversation dialog and the message composer so the two
// authoring surfaces can't drift. `allowedKeys === null` means the evaluation leaves tags free-form
// (free text); a list means it restricts them, and only those keys are offered.
export function TagKeyField({
  value,
  onChange,
  allowedKeys,
  keysStatus,
  takenKeys,
  ariaLabel,
  className,
  autoFocus,
  maxLength,
  disabled,
}: {
  value: string
  onChange: (key: string) => void
  allowedKeys: string[] | null
  // While the query is unsettled no key can be judged stale, and an empty list is not proof that
  // nothing is allowed.
  keysStatus: AllowedKeysStatus
  // Keys the row's siblings already use — excluded from the options, since the saved map is keyed
  // and a duplicate would silently overwrite instead of adding a tag.
  takenKeys: string[]
  ariaLabel: string
  className?: string
  // Free-text branch only: a select needs neither, since its options are already legal keys.
  autoFocus?: boolean
  maxLength?: number
  disabled?: boolean
}) {
  if (allowedKeys === null)
    return (
      <Input
        aria-label={ariaLabel}
        autoFocus={autoFocus}
        maxLength={maxLength}
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="key"
        className={className}
      />
    )
  const options = allowedKeys.filter((key) => !takenKeys.includes(key))
  // A key already on the tag map but absent from the offered list stays selectable so it isn't
  // silently dropped from a full-map write - and so the row still shows which tag it is. Without it
  // the trigger shows the "no key" row, which reads as a tag with no key at all.
  const ownKeyMissing = value !== '' && !options.includes(value)
  // Judged against the allow-list, not against `options`: `options` also excludes a sibling row's
  // key, so deriving the label from it would call an allowed key "no longer allowed". And only a
  // settled list can say a key was withdrawn (the backend rejects the whole write until it goes) —
  // pending or failed, the row's key is offered unlabelled, since an empty list proves nothing.
  const staleKey = ownKeyMissing && keysStatus === 'success' && !allowedKeys.includes(value)
  return (
    <Select
      value={value === '' ? EMPTY_VALUE : value}
      onValueChange={(next) => onChange(next === EMPTY_VALUE ? '' : next)}
      disabled={disabled}
    >
      <SelectTrigger aria-label={ariaLabel} className={className}>
        {/* No `placeholder`: the sentinel means the value is never empty, so Radix would never
            reach it. The "no key" row carries the muted styling itself instead. */}
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {/* Radix rejects an empty `SelectItem` value, so clearing back to no key travels as a
              sentinel. Without it the placeholder is unreachable once a key has been picked, and
              the only way back out of a row would be to delete the row. */}
          <SelectItem value={EMPTY_VALUE} className="text-muted-foreground">
            — select key —
          </SelectItem>
          {ownKeyMissing && (
            <SelectItem value={value}>
              {staleKey ? `${value} (no longer allowed)` : value}
            </SelectItem>
          )}
          {options.map((key) => (
            <SelectItem key={key} value={key}>
              {key}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
