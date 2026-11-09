import { useRef, useState } from 'react'
import { ImageOff } from 'lucide-react'
import { humanizeError } from '@/lib/api/problem'
import { IMAGE_TYPES, publicImageUrl, uploadPublicImage, validateImageFile } from '@/lib/api/images'
import { Button } from '@/components/ui/button'

// Uploads on pick, so the form submits just the key. A replaced or abandoned upload is left to the
// backend's orphan reaper: clones share the key, so deleting it here could break another evaluation.
export function CoverImageField({
  value,
  onChange,
  onUploadingChange,
}: {
  value: string
  onChange: (key: string) => void
  onUploadingChange: (uploading: boolean) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Keyed to the value, so a new cover gets a fresh chance to load.
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
    <div className="space-y-2">
      {value &&
        (brokenKey === value ? (
          <div className="text-muted-foreground flex h-32 w-56 items-center justify-center gap-2 rounded-md border text-sm">
            <ImageOff className="size-4" aria-hidden="true" />
            Current cover can’t be displayed
          </div>
        ) : (
          <img
            src={publicImageUrl(value)}
            alt="Cover image"
            className="h-32 w-56 rounded-md border object-cover"
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
