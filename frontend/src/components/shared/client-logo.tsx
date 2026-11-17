import { useState, type ReactNode } from 'react'
import { publicImageUrl } from '@/lib/api/images'
import { cn } from '@/lib/utils'

// Both variants are rendered and CSS picks one, so a theme switch needs no re-render or re-fetch.
// A key can outlive its image (the uploader may delete it), so a failed load shows `fallback`
// instead of a broken-image icon.
export function ClientLogo({
  light,
  dark,
  alt,
  className,
  fallback = null,
}: {
  light: string
  dark: string
  alt: string
  className?: string
  fallback?: ReactNode
}) {
  const pair = `${light}|${dark}`
  const [failed, setFailed] = useState<string | null>(null)
  if (failed === pair) return <>{fallback}</>
  const onError = () => setFailed(pair)
  return (
    <>
      <img
        src={publicImageUrl(light)}
        alt={alt}
        onError={onError}
        className={cn('object-contain object-left dark:hidden', className)}
      />
      <img
        src={publicImageUrl(dark)}
        alt={alt}
        onError={onError}
        className={cn('hidden object-contain object-left dark:block', className)}
      />
    </>
  )
}
