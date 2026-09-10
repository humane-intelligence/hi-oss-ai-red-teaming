import { apiClient, signalConsentRefusal, UNAUTHORIZED_EVENT } from '@/lib/api/client'
import { ApiError, readProblem } from '@/lib/api/problem'
import { unwrap } from '@/lib/api/fetcher'
import { tokenStore } from '@/lib/auth/token-store'
import type { MediaAssetResponse } from '@/lib/api/types'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

// Mirror of the backend upload limits so a doomed file is rejected before the request.
export const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']
export const IMAGE_MAX_BYTES = 20 * 1024 * 1024

export function validateImageFile(file: File): string | null {
  if (!IMAGE_TYPES.includes(file.type)) return 'Only JPEG, PNG, or WebP images can be attached.'
  if (file.size > IMAGE_MAX_BYTES) return 'Image is larger than 20 MB.'
  return null
}

// Composer attachments upload private: conversation images are adversarial content,
// and the backend rejects a public key on attach anyway.
export async function uploadPrivateImage(file: File): Promise<MediaAssetResponse> {
  return unwrap(
    await apiClient.POST('/api/v1/images', {
      // The generated type renders the binary part as `file: string`; the wire format is FormData.
      body: { file, is_private: true } as unknown as { file: string; is_private: boolean },
      bodySerializer: (body) => {
        const fd = new FormData()
        fd.append('file', body.file as unknown as File)
        fd.append('is_private', String(body.is_private))
        return fd
      },
    }),
  )
}

/**
 * Fetch a (private) image's bytes as a Blob: mint a signed URL, then fetch it with the
 * bearer header. A user-bound signed URL can't ride `<img src>` — the browser sends no
 * Authorization header from localStorage — so display goes through an object URL
 * (mirrors `downloadFile`).
 */
export async function fetchImageBlob(key: string): Promise<Blob> {
  const { url } = unwrap(
    await apiClient.GET('/api/v1/images/signed-url', { params: { query: { key } } }),
  )
  const token = tokenStore.get()
  const response = await fetch(`${BASE}${url}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    if (response.status === 401 && token) {
      tokenStore.clear()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    // Parsed once, and an `ApiError`, for the same reasons as in `download.ts`.
    const problem = await readProblem(response)
    signalConsentRefusal(response.status, problem)
    throw new ApiError(problem, response.status)
  }
  return response.blob()
}
