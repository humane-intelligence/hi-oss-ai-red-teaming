import { useRef } from 'react'
import { Loader2, Paperclip, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { IMAGE_TYPES } from '@/lib/api/images'
import { MAX_ATTACHMENTS, type Attachment } from './use-message-attachments'

export function AttachmentStrip({
  attachments,
  onAdd,
  onRemove,
  disabled,
}: {
  attachments: Attachment[]
  onAdd: (files: File[]) => void
  onRemove: (id: string) => void
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const full = attachments.length >= MAX_ATTACHMENTS
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        ref={inputRef}
        type="file"
        accept={IMAGE_TYPES.join(',')}
        multiple
        className="hidden"
        aria-hidden
        tabIndex={-1}
        onChange={(e) => {
          onAdd(Array.from(e.target.files ?? []))
          e.target.value = '' // allow re-picking a file that was just removed
        }}
      />
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={disabled || full}
        title={full ? `Up to ${MAX_ATTACHMENTS} images per message` : 'Attach images'}
        aria-label="Attach images"
        onClick={() => inputRef.current?.click()}
      >
        <Paperclip className="size-4" />
        {attachments.length > 0 && (
          <span className="text-xs tabular-nums">
            {attachments.length}/{MAX_ATTACHMENTS}
          </span>
        )}
      </Button>
      {attachments.map((a) => (
        <div
          key={a.id}
          title={a.error ?? a.file.name}
          className={cn(
            'relative size-12 shrink-0 overflow-hidden rounded-md border',
            a.status === 'error' && 'border-destructive',
          )}
        >
          <img src={a.previewUrl} alt={a.file.name} className="size-full object-cover" />
          {a.status === 'uploading' && (
            <div className="absolute inset-0 grid place-items-center bg-black/40">
              <Loader2 className="size-4 animate-spin text-white" />
            </div>
          )}
          {a.status === 'error' && (
            <div className="bg-destructive/90 absolute inset-x-0 bottom-0 text-center text-[9px] font-medium text-white">
              failed
            </div>
          )}
          <button
            type="button"
            aria-label={`Remove ${a.file.name}`}
            disabled={disabled}
            onClick={() => onRemove(a.id)}
            className="absolute top-0 right-0 grid size-4 place-items-center rounded-bl-md bg-black/60 text-white hover:bg-black/80 disabled:opacity-50"
          >
            <X className="size-3" />
          </button>
        </div>
      ))}
    </div>
  )
}
