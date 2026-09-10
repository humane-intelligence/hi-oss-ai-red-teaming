import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { uploadPrivateImage, validateImageFile } from '@/lib/api/images'
import { uuid } from '@/lib/uuid'

export const MAX_ATTACHMENTS = 5

export type Attachment = {
  id: string
  file: File
  previewUrl: string
  status: 'uploading' | 'ready' | 'error'
  key?: string
  error?: string
}

/**
 * Composer attachment state: files upload (private) as soon as they're picked, so by
 * send time only the storage keys remain to attach. Uploads deliberately bypass
 * useMutation — nothing to invalidate, and failures surface inline on the thumbnail,
 * not as a toast.
 */
export function useMessageAttachments() {
  const qc = useQueryClient()
  const [attachments, setAttachments] = useState<Attachment[]>([])
  // Object URLs aren't GC'd with the component — track for unmount revocation.
  const urls = useRef<Set<string>>(new Set())

  useEffect(() => {
    const owned = urls.current
    return () => owned.forEach((u) => URL.revokeObjectURL(u))
  }, [])

  const patch = (id: string, changes: Partial<Attachment>) =>
    setAttachments((list) => list.map((a) => (a.id === id ? { ...a, ...changes } : a)))

  const add = (files: File[]) => {
    const room = MAX_ATTACHMENTS - attachments.length
    if (files.length > room) toast.error(`Up to ${MAX_ATTACHMENTS} images per message.`)
    const accepted: Attachment[] = []
    for (const file of files.slice(0, Math.max(0, room))) {
      const invalid = validateImageFile(file)
      if (invalid) {
        toast.error(invalid)
        continue
      }
      const previewUrl = URL.createObjectURL(file)
      urls.current.add(previewUrl)
      accepted.push({ id: uuid(), file, previewUrl, status: 'uploading' })
    }
    if (accepted.length === 0) return
    setAttachments((list) => [...list, ...accepted])
    for (const item of accepted) {
      void uploadPrivateImage(item.file)
        .then((asset) => {
          // Seed the display cache with the local bytes — the just-sent message's
          // thumbnails then render without re-fetching what we just uploaded.
          qc.setQueryData(['image-blob', asset.key], item.file)
          patch(item.id, { status: 'ready', key: asset.key })
        })
        .catch((err: unknown) =>
          patch(item.id, {
            status: 'error',
            error: err instanceof Error ? err.message : 'Upload failed',
          }),
        )
    }
  }

  const release = (item: Attachment) => {
    URL.revokeObjectURL(item.previewUrl)
    urls.current.delete(item.previewUrl)
  }

  const remove = (id: string) => {
    const item = attachments.find((a) => a.id === id)
    if (item) release(item)
    setAttachments((list) => list.filter((a) => a.id !== id))
  }

  const clear = () => {
    attachments.forEach(release)
    setAttachments([])
  }

  // Re-hydrate the strip after a failed send. clear() revoked the preview URLs but the
  // underlying Files (and their ready upload keys) survive, so mint fresh preview URLs.
  const restore = (list: Attachment[]) => {
    const revived = list.map((a) => {
      const previewUrl = URL.createObjectURL(a.file)
      urls.current.add(previewUrl)
      return { ...a, previewUrl }
    })
    setAttachments(revived)
  }

  return {
    attachments,
    add,
    remove,
    clear,
    restore,
    keys: attachments.flatMap((a) => (a.status === 'ready' && a.key ? [a.key] : [])),
    uploading: attachments.some((a) => a.status === 'uploading'),
    failed: attachments.some((a) => a.status === 'error'),
  }
}
