import { useState } from 'react'
import { Image as ImageIcon } from 'lucide-react'

import { publicImageUrl } from '@/lib/api/images'
import { cn } from '@/lib/utils'

type Props = {
  imageKey: string | null | undefined
  alt?: string
  className?: string
  // A list thumbnail sits next to the title link, so announcing it on every row adds nothing.
  decorative?: boolean
}

export function EvaluationCover({ imageKey, alt = '', className, decorative = false }: Props) {
  const [brokenKey, setBrokenKey] = useState<string | null>(null)
  const showImage = !!imageKey && brokenKey !== imageKey
  return (
    <div
      className={cn(
        'bg-muted flex shrink-0 items-center justify-center overflow-hidden rounded-md border',
        className,
      )}
    >
      {showImage ? (
        <img
          src={publicImageUrl(imageKey)}
          alt={decorative ? '' : alt}
          loading={decorative ? 'lazy' : 'eager'}
          className="max-h-full max-w-full object-contain"
          onError={() => setBrokenKey(imageKey)}
        />
      ) : decorative ? (
        <ImageIcon className="text-muted-foreground size-4" aria-hidden="true" />
      ) : (
        <div
          role="img"
          aria-label="No cover image"
          className="text-muted-foreground flex flex-col items-center gap-1 text-xs"
        >
          <ImageIcon className="size-5" aria-hidden="true" />
          No cover image
        </div>
      )}
    </div>
  )
}
