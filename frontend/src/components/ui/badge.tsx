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
        ok: 'border-transparent bg-ok-surface text-ok',
        warn: 'border-transparent bg-warn-surface text-warn',
        new: 'border-transparent bg-new-surface text-new',
        err: 'border-transparent bg-err-surface text-err',
        approved: 'border-transparent bg-approved-surface text-approved',
        changes: 'border-transparent bg-changes-surface text-changes',
        muted: 'border-transparent bg-muted/60 text-muted-foreground',
        dashed: 'border-dashed border-muted-foreground/60 bg-transparent text-foreground',
        neutral: 'border-border bg-muted text-muted-foreground',
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
