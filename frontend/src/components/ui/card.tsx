import type { HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('bg-card text-card-foreground rounded-xl border shadow-sm', className)}
      {...props}
    />
  )
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col gap-1 p-6', className)} {...props} />
}

// `as`: a card that sits under a section heading is itself a heading; upstream only renders a div.
export function CardTitle({
  as: Tag = 'div',
  className,
  ...props
}: HTMLAttributes<HTMLHeadingElement> & { as?: 'div' | 'h3' }) {
  return <Tag className={cn('leading-none font-semibold tracking-tight', className)} {...props} />
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-6 pt-0', className)} {...props} />
}
