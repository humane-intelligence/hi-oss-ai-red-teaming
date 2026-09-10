import type { HTMLAttributes } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        outline: 'text-foreground',
        destructive: 'border-transparent bg-destructive text-destructive-foreground',
        ok: 'border-ok/40 bg-ok/15 text-ok font-mono uppercase tracking-wide',
        warn: 'border-warn/40 bg-warn/15 text-warn font-mono uppercase tracking-wide',
        err: 'border-err/40 bg-err/15 text-err font-mono uppercase tracking-wide',
        neutral: 'border-border bg-muted text-muted-foreground font-mono uppercase tracking-wide',
        // Tag keys and values reach the model verbatim, so no case transform misreports them.
        tag: 'border-border bg-muted text-muted-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

export type BadgeProps = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
