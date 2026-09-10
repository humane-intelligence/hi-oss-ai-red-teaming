import { cloneElement, Fragment, isValidElement, useId, type ReactNode } from 'react'
import { InfoHint } from '@/components/shared/info-hint'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'

// `Field` renders a description and an error but wires neither to the control, so every caller had
// to remember `aria-describedby` itself - and the system preferences checkboxes show what happens
// when one doesn't. Wired here instead, by cloning the control the caller passed as `children`.
//
// This only reaches a single child that puts unknown props on a DOM node: a host element, or a
// component like `Input` that spreads them. A child that renders through a prop (`Controller`) or
// keeps its own prop list swallows them silently, so those callers pass `errorId` and wire the real
// control themselves.
const wire = (children: ReactNode, describedBy: string | undefined, invalid: boolean) => {
  if (!describedBy && !invalid) return children
  // `children` directly, never `Children.toArray`: that rewrites the key to ".0", so the element
  // returned while an error stands has a different key from the one returned without it, and React
  // unmounts and remounts the control on every transition - losing focus mid-correction.
  const only = isValidElement<{ 'aria-describedby'?: string; 'aria-invalid'?: boolean }>(children)
    ? children
    : undefined
  if (!only) return children
  // A fragment would accept the props and drop them, so the wiring would vanish silently. Leave
  // it, and the field's own `htmlFor`/error copy still stand on their own.
  if (only.type === Fragment) return children
  const existing = only.props['aria-describedby']
  const ids = [...new Set([existing, describedBy].filter(Boolean).join(' ').split(' '))]
    .filter(Boolean)
    .join(' ')
  return cloneElement(only, {
    ...(ids ? { 'aria-describedby': ids } : {}),
    // Never overrides a caller that set it deliberately (a field invalid for a reason of its own).
    ...(invalid && only.props['aria-invalid'] === undefined ? { 'aria-invalid': true } : {}),
  })
}

export function FormField({
  label,
  htmlFor,
  error,
  hint,
  description,
  descriptionId,
  errorId,
  orientation,
  children,
}: {
  label: string
  htmlFor?: string
  error?: string
  // Rendered as an info popover beside the label, for copy too long to sit inline.
  hint?: string
  // Rendered under the control, for copy that should always be visible.
  description?: ReactNode
  // Only needed when something outside this field has to reference the description, or when the
  // child cannot be wired automatically (see `wire`) and the caller does it by hand.
  descriptionId?: string
  // Same, for the error: a caller whose child swallows cloned props needs a stable id to point at.
  errorId?: string
  orientation?: 'vertical' | 'horizontal'
  children: ReactNode
}) {
  const generatedId = useId()
  const ownErrorId = errorId ?? `${generatedId}-error`
  const ownDescriptionId = descriptionId ?? `${generatedId}-description`
  const describedBy =
    [description ? ownDescriptionId : undefined, error ? ownErrorId : undefined]
      .filter(Boolean)
      .join(' ') || undefined

  return (
    <Field orientation={orientation} data-invalid={error ? true : undefined}>
      {/* The hint sits beside the label, not inside it: nesting changes the label's text and
          `getByLabelText` no longer matches the field. */}
      <div className="flex items-center gap-1.5">
        <FieldLabel htmlFor={htmlFor}>{label}</FieldLabel>
        {hint && <InfoHint text={hint} />}
      </div>
      {wire(children, describedBy, Boolean(error))}
      {description && <FieldDescription id={ownDescriptionId}>{description}</FieldDescription>}
      {error && <FieldError id={ownErrorId}>{error}</FieldError>}
    </Field>
  )
}
