import type { Ref, SelectHTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

// A bare styled `<select>`, deliberately not the registry's `native-select` (which wraps the
// element in a div with its own chevron and `size` contract) so `npx shadcn add` cannot clobber it.
//
// Four callers keep it because they spread `{...register(...)}` straight onto the element: that
// returns `name`/`onChange`/`onBlur`/`ref` for a DOM node, and Radix's Root takes
// `value`/`onValueChange`, so moving them needs a `Controller` first.
export function PlainSelect({
  className,
  ref,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement> & { ref?: Ref<HTMLSelectElement> }) {
  return (
    <select
      ref={ref}
      className={cn(
        // Styled native `<select>`; kept for its real mobile picker wheels.
        // `min-w-0`: a select's intrinsic minimum is its widest option, which `w-full` does not override.
        'bg-card text-card-foreground focus-visible:border-ring focus-visible:ring-ring [&>option]:bg-card [&>option]:text-card-foreground flex h-9 w-full min-w-0 rounded-md border px-3 text-sm shadow-sm focus-visible:ring-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    />
  )
}
