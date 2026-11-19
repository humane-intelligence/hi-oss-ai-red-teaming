import { useState } from 'react'
import { Image as ImageIcon } from 'lucide-react'

import { publicImageUrl } from '@/lib/api/images'
import { cn } from '@/lib/utils'

type Props = {
  imageKey: string | null | undefined
  alt: string
  className?: string
  placeholderLabel?: string
}

export function EvaluationCover({ imageKey, alt, className, placeholderLabel }: Props) {
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
          alt={alt}
          loading="lazy"
          className="max-h-full max-w-full object-contain"
          onError={() => setBrokenKey(imageKey)}
        />
      ) : (
        <div
          role="img"
          aria-label={placeholderLabel ?? 'No cover image'}
          className="text-muted-foreground flex flex-col items-center gap-1 text-xs"
        >
          <ImageIcon className="size-5" aria-hidden="true" />
          {placeholderLabel}
        </div>
      )}
    </div>
  )
}
