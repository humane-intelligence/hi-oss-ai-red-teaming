import type { ComponentProps } from 'react'
import { X } from 'lucide-react'
import { FormField } from '@/components/shared/form-field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'

// Whatever props drive the host's input — `register()`'s return, or a controlled
// `{value, onChange}`. The attributes this field owns are omitted rather than merely
// overwritten, so a host cannot silently break the label's `htmlFor` by passing `id`.
type BoundInput = Omit<ComponentProps<typeof Input>, 'id' | 'type' | 'step'>

function ClearButton({
  label,
  filled,
  onClear,
}: {
  label: string
  filled: boolean
  onClear: () => void
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      // Kept mounted, hidden via `visibility`, so the row does not reflow as values
      // come and go. `invisible` also drops it from the a11y tree and tab order, so
      // no `aria-hidden` is needed — and adding one would leave it focusable.
      className={filled ? undefined : 'invisible'}
      aria-label={`Clear ${label}`}
      onClick={onClear}
    >
      <X className="size-4" />
    </Button>
  )
}

// One inference knob's input, shared by every panel that edits the cascade.
//
// The panels disagree on state: the model and assignment forms bind through
// react-hook-form, the group creator keeps a controlled value per row. Rather
// than teach one component both, the host passes whatever props drive its input
// and says how to blank the field — so the affordances here are written once
// without unifying the state models.
export function ParamField({
  id,
  label,
  hint,
  step,
  error,
  inherited,
  filled,
  onClear,
  inputProps,
}: {
  id: string
  label: string
  hint?: string
  step: string
  error?: string
  // What this field falls back to when left blank, already formatted. Rendered as
  // its own line rather than a placeholder: a placeholder reads as "nothing is set
  // here" and disappears the moment you type, which is exactly when you want to
  // compare your value against what you are overriding.
  inherited?: string
  // Whether the input holds a value — the host owns it, so it has to say. Note a
  // legitimate `0` is the string "0" here (form values are strings), so this must
  // stay an emptiness test, not a truthiness one.
  filled: boolean
  onClear: () => void
  inputProps: BoundInput
}) {
  return (
    <FormField label={label} htmlFor={id} hint={hint} error={error}>
      <div className="flex items-center gap-1">
        <Input {...inputProps} id={id} type="number" step={step} />
        <ClearButton label={label} filled={filled} onClear={onClear} />
      </div>
      {inherited && (
        // Clamped with the full value on hover: a system prompt runs to hundreds of
        // characters and this line sits inside a modal, where the old placeholder was
        // clipped by the input and this paragraph would not be.
        <p className="text-muted-foreground mt-1 line-clamp-2 text-xs" title={inherited}>
          Inherited: {inherited}
        </p>
      )}
    </FormField>
  )
}

// The system prompt shares the knob vocabulary but not the input: a textarea, no
// step. Same clear + inherited affordances, so both panels get them once.
export function SystemPromptField({
  id,
  hint,
  inherited,
  filled,
  onClear,
  inputProps,
}: {
  id: string
  hint?: string
  inherited?: string
  filled: boolean
  onClear: () => void
  inputProps: Omit<ComponentProps<typeof Textarea>, 'id'>
}) {
  return (
    <FormField label="System prompt" htmlFor={id} hint={hint}>
      <div className="flex items-start gap-1">
        <Textarea {...inputProps} id={id} />
        <ClearButton label="System prompt" filled={filled} onClear={onClear} />
      </div>
      {inherited && (
        // Clamped with the full value on hover: a system prompt runs to hundreds of
        // characters and this line sits inside a modal, where the old placeholder was
        // clipped by the input and this paragraph would not be.
        <p className="text-muted-foreground mt-1 line-clamp-2 text-xs" title={inherited}>
          Inherited: {inherited}
        </p>
      )}
    </FormField>
  )
}
