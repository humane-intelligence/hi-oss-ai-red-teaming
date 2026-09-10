import type { InputHTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        // `min-w-0`: `w-full` does not override `min-width: auto`, so inputs could not shrink.
        'bg-card text-card-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring flex h-9 w-full min-w-0 rounded-md border px-3 py-1 text-sm shadow-sm transition-colors focus-visible:ring-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    />
  )
}
