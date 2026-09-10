import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, ImageOff, Loader2 } from 'lucide-react'
import { Modal } from '@/components/ui/modal'
import { fetchImageBlob } from '@/lib/api/images'
import { cn } from '@/lib/utils'

// ~the signed-URL TTL: a deleted/reaped blob stops resolving on the next refetch window.
const BLOB_CACHE_MS = 15 * 60 * 1000

/**
 * Resolve a private image to a displayable object URL. Private attachments can't ride
 * `<img src>` (the user-bound signed URL needs the bearer header), so the blob is
 * fetched authenticated and handed to the browser as an object URL, revoked on unmount.
 */
function useImageObjectUrl(imageKey: string) {
  const query = useQuery({
    queryKey: ['image-blob', imageKey],
    queryFn: () => fetchImageBlob(imageKey),
    staleTime: BLOB_CACHE_MS,
    gcTime: BLOB_CACHE_MS,
    retry: false,
    // A vanished attachment (uploader delete / retention reaper) renders as a
    // placeholder right where the user looks — a toast would just be noise.
    meta: { suppressErrorToast: true },
  })
  const blob = query.data
  const [url, setUrl] = useState<string | null>(null)
  // Create the object URL in the effect, not in render: a render-phase create is
  // revoked by its own cleanup on StrictMode's mount/unmount/mount, leaving the
  // committed <img> pointing at a dead URL (broken thumbnail in dev). Tying create
  // and revoke to the same committed effect keeps the shown URL live.
  useEffect(() => {
    const objectUrl = blob ? URL.createObjectURL(blob) : null
    // One set per blob (not a render loop) — syncing an external resource that also
    // needs cleanup is the sanctioned use of setState-in-effect the rule can't tell apart.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setUrl(objectUrl)
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [blob])
  return { url, isError: query.isError }
}

function AttachmentThumb({
  imageKey,
  index,
  onOpen,
}: {
  imageKey: string
  index: number
  onOpen: () => void
}) {
  const { url, isError } = useImageObjectUrl(imageKey)
  if (isError) {
    return (
      <div
        role="img"
        aria-label="Attachment unavailable"
        title="Attachment unavailable"
        className="bg-muted text-muted-foreground grid size-16 place-items-center rounded-md border"
      >
        <ImageOff className="size-4" />
      </div>
    )
  }
  if (!url) {
    return (
      <div className="bg-muted grid size-16 place-items-center rounded-md border">
        <Loader2 className="text-muted-foreground size-4 animate-spin" />
      </div>
    )
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`View attachment ${index + 1} full size`}
      className="overflow-hidden rounded-md border transition-opacity hover:opacity-80"
    >
      <img src={url} alt={`Attachment ${index + 1}`} className="size-16 object-cover" />
    </button>
  )
}

// Own hook instance (not a snapshot of the thumb's URL): the thumb's object URL is
// revoked whenever its blob refetches, which would blank an open lightbox.
function LightboxImage({ imageKey, index }: { imageKey: string; index: number }) {
  const { url, isError } = useImageObjectUrl(imageKey)
  if (isError) {
    return <p className="text-muted-foreground text-sm">Attachment unavailable.</p>
  }
  if (!url) {
    return (
      <div className="grid h-40 place-items-center">
        <Loader2 className="text-muted-foreground size-5 animate-spin" />
      </div>
    )
  }
  return (
    <img
      src={url}
      alt={`Attachment ${index + 1} full size`}
      className="max-h-[70svh] w-full rounded-md object-contain"
    />
  )
}

export function MessageAttachments({
  imageKeys,
  className,
}: {
  imageKeys: string[]
  className?: string
}) {
  const [openIndex, setOpenIndex] = useState<number | null>(null)
  const count = imageKeys.length
  const step = useCallback(
    (delta: number) =>
      setOpenIndex((i) => (i === null ? i : Math.min(count - 1, Math.max(0, i + delta)))),
    [count],
  )

  // Keyed on open/closed, not the exact index, so navigating doesn't re-subscribe the listener.
  const lightboxOpen = openIndex !== null
  useEffect(() => {
    if (!lightboxOpen || count < 2) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') {
        e.preventDefault()
        step(1)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        step(-1)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightboxOpen, count, step])

  if (count === 0) return null
  const openKey = openIndex !== null ? imageKeys[openIndex] : undefined
  return (
    <>
      <div className={cn('flex flex-wrap gap-1.5', className)}>
        {imageKeys.map((key, index) => (
          <AttachmentThumb
            key={`${key}-${index}`}
            imageKey={key}
            index={index}
            onOpen={() => setOpenIndex(index)}
          />
        ))}
      </div>
      <Modal
        open={openIndex !== null}
        onOpenChange={(o) => {
          if (!o) setOpenIndex(null)
        }}
        title={`Attachment ${(openIndex ?? 0) + 1} of ${count}`}
        dismissable
        className="max-w-3xl"
      >
        {openIndex !== null && openKey !== undefined && (
          <div className="flex items-center gap-2">
            {count > 1 && (
              <button
                type="button"
                aria-label="Previous attachment"
                disabled={openIndex === 0}
                onClick={() => step(-1)}
                className="bg-card hover:bg-accent grid size-9 shrink-0 place-items-center rounded-md border disabled:pointer-events-none disabled:opacity-40"
              >
                <ChevronLeft className="size-5" />
              </button>
            )}
            <div className="min-w-0 flex-1">
              <LightboxImage imageKey={openKey} index={openIndex} />
            </div>
            {count > 1 && (
              <button
                type="button"
                aria-label="Next attachment"
                disabled={openIndex === count - 1}
                onClick={() => step(1)}
                className="bg-card hover:bg-accent grid size-9 shrink-0 place-items-center rounded-md border disabled:pointer-events-none disabled:opacity-40"
              >
                <ChevronRight className="size-5" />
              </button>
            )}
          </div>
        )}
      </Modal>
    </>
  )
}
