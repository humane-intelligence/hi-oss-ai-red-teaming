import { useEffect, useState, type ReactNode } from 'react'
import { publicImageUrl } from '@/lib/api/images'
import { cn } from '@/lib/utils'

// Both variants are rendered and CSS picks one, so a theme switch needs no re-render or re-fetch.
// A key can outlive its image (the uploader may delete it), so a variant that fails to load borrows
// the other's key, and `fallback` shows only once both have failed — never a broken-image icon.
export function ClientLogo({
  light,
  dark,
  alt,
  className,
  fallback = null,
  onReady,
}: {
  light: string
  dark: string
  alt: string
  className?: string
  fallback?: ReactNode
  // Called once both slots have an image to show or the fallback is up, i.e. the size is final.
  onReady?: () => void
}) {
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set())
  const [loaded, setLoaded] = useState<ReadonlySet<string>>(new Set())
  const lightSrc = failed.has(light) ? dark : light
  const darkSrc = failed.has(dark) ? light : dark
  const bothFailed = failed.has(lightSrc) && failed.has(darkSrc)
  const ready = bothFailed || (loaded.has(lightSrc) && loaded.has(darkSrc))
  useEffect(() => {
    if (ready) onReady?.()
  }, [ready, onReady])
  if (bothFailed) return <>{fallback}</>
  const onError = (key: string) => () => setFailed((prev) => new Set(prev).add(key))
  const onLoad = (key: string) => () => setLoaded((prev) => new Set(prev).add(key))
  return (
    <>
      <img
        src={publicImageUrl(lightSrc)}
        alt={alt}
        onError={onError(lightSrc)}
        onLoad={onLoad(lightSrc)}
        className={cn('block object-contain object-left dark:hidden', className)}
      />
      <img
        src={publicImageUrl(darkSrc)}
        alt={alt}
        onError={onError(darkSrc)}
        onLoad={onLoad(darkSrc)}
        className={cn('hidden object-contain object-left dark:block', className)}
      />
    </>
  )
}
