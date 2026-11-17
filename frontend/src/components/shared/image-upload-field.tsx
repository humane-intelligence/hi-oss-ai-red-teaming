import { useRef, useState } from 'react'
import { ImageOff } from 'lucide-react'
import { humanizeError } from '@/lib/api/problem'
import { IMAGE_TYPES, publicImageUrl, uploadPublicImage, validateImageFile } from '@/lib/api/images'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

// Uploads on pick, so the form submits just the key. A replaced or abandoned upload is left to the
// backend's orphan reaper: a key can be shared (evaluation clones share a cover), so deleting it here
// could break another reference.
export function ImageUploadField({
  value,
  onChange,
  onUploadingChange,
  labelId,
  alt,
  noun,
  previewClassName,
  'aria-describedby': describedBy,
}: {
  value: string
  onChange: (key: string) => void
  // Alt text of the preview, and what the field calls its image ("cover", "logo").
  alt: string
  noun: string
  previewClassName: string
  onUploadingChange: (uploading: boolean) => void
  labelId: string
  // Forwarded by `FormField`; the group carries it because no single control owns the field.
  'aria-describedby'?: string
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Keyed to the value, so a new image gets a fresh chance to load.
  const [brokenKey, setBrokenKey] = useState<string | null>(null)

  const setBusy = (busy: boolean) => {
    setUploading(busy)
    onUploadingChange(busy)
  }

  const pick = async (file: File | undefined) => {
    if (!file) return
    const invalid = validateImageFile(file)
    setError(invalid)
    if (invalid) return
    setBusy(true)
    try {
      onChange((await uploadPublicImage(file)).key)
    } catch (err) {
      setError(humanizeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      role="group"
      aria-labelledby={labelId}
      aria-describedby={describedBy}
      className="space-y-2"
    >
      {value &&
        (brokenKey === value ? (
          <div
            className={cn(
              'text-muted-foreground flex flex-col items-center justify-center gap-2 rounded-md border p-3 text-center text-sm',
              previewClassName,
            )}
          >
            <ImageOff className="size-4" aria-hidden="true" />
            Current {noun} can’t be displayed
          </div>
        ) : (
          <img
            src={publicImageUrl(value)}
            alt={alt}
            className={cn('rounded-md border', previewClassName)}
            onError={() => setBrokenKey(value)}
          />
        ))}
      <div className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {uploading ? 'Uploading…' : value ? 'Replace image' : 'Upload image'}
        </Button>
        {value && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={uploading}
            onClick={() => {
              setError(null)
              onChange('')
            }}
          >
            Remove image
          </Button>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={IMAGE_TYPES.join(',')}
        className="hidden"
        aria-hidden="true"
        tabIndex={-1}
        onChange={(e) => {
          void pick(e.target.files?.[0])
          e.target.value = ''
        }}
      />
      {error && (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      )}
    </div>
  )
}
