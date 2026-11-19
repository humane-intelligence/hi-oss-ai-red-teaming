import type { ReactNode } from 'react'

// `className` lands on the wrapper, so a caller inside a grid can widen one entry.
export function Field({
  label,
  children,
  className,
  inline = false,
}: {
  label: string
  children: ReactNode
  className?: string
  /** "Label: value" on one line instead of the label above the value. */
  inline?: boolean
}) {
  if (inline) {
    return (
      <div className={className}>
        <dt className="inline text-sm font-semibold">{label}:</dt>{' '}
        <dd className="inline text-sm break-words">{children}</dd>
      </div>
    )
  }
  return (
    <div className={className}>
      <dt className="text-muted-foreground text-xs tracking-wide">{label}</dt>
      <dd className="mt-0.5 text-sm break-words">{children}</dd>
    </div>
  )
}
