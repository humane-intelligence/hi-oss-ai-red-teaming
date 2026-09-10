import type { InputHTMLAttributes, ReactNode } from 'react'

// Presentational only — the caller spreads `register(...)` (or `checked`/`onChange`) onto it, so
// there are no form generics and no casts. Keeps the label/description/error wiring in one place
// across the three surfaces that collect consent.
export function ConsentCheckbox({
  id,
  label,
  hint,
  error,
  extra,
  ...input
}: {
  id: string
  label: string
  hint: ReactNode
  error?: string
  // Rendered under the hint, outside the described paragraph — a focusable element inside a
  // description becomes part of the control's accessible name.
  extra?: ReactNode
} & InputHTMLAttributes<HTMLInputElement>) {
  const hintId = `${id}-hint`
  return (
    <div className="flex items-start gap-2 text-sm">
      {/* The hint is a sibling folded into `aria-describedby`, not inside the label: text in the
          label becomes the checkbox's accessible name. */}
      <input
        id={id}
        type="checkbox"
        className="mt-0.5 size-4 rounded border"
        aria-describedby={hintId}
        {...input}
      />
      <div>
        <label htmlFor={id}>{label}</label>
        <p id={hintId} className="text-muted-foreground block text-xs">
          {hint}
        </p>
        {extra && <div className="mt-1 text-xs">{extra}</div>}
        {error && (
          <p role="alert" className="text-destructive mt-1 text-xs">
            {error}
          </p>
        )}
      </div>
    </div>
  )
}
