import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/msw/server'
import { tokenStore } from '@/lib/auth/token-store'
import { fetchImageBlob, uploadPrivateImage, validateImageFile } from './images'

const ASSET = {
  id: 'a0000000-0000-0000-0000-000000000001',
  key: '2026/07/17/abc.png',
  url: '/api/v1/images/2026/07/17/abc.png',
  content_type: 'image/png',
  width: 10,
  height: 10,
  size_bytes: 68,
  is_private: true,
  created_at: '2026-07-17T00:00:00Z',
}

describe('validateImageFile', () => {
  it('rejects a non-image type and an oversized file, accepts a small png', () => {
    expect(validateImageFile(new File(['x'], 'a.txt', { type: 'text/plain' }))).toMatch(/JPEG/)
    const big = new File([new Uint8Array(1)], 'big.png', { type: 'image/png' })
    Object.defineProperty(big, 'size', { value: 21 * 1024 * 1024 })
    expect(validateImageFile(big)).toMatch(/20 MB/)
    expect(validateImageFile(new File(['x'], 'a.png', { type: 'image/png' }))).toBeNull()
  })
})

describe('uploadPrivateImage', () => {
  it('posts multipart form data with is_private=true and returns the asset', async () => {
    // Asserted on the raw body: undici's multipart parser chokes on jsdom's FormData
    // (test-env mismatch only), so request.formData() is unusable here.
    let contentType: string | null = null
    let rawBody = ''
    server.use(
      http.post('http://localhost/api/v1/images', async ({ request }) => {
        contentType = request.headers.get('Content-Type')
        rawBody = await request.text()
        return HttpResponse.json(ASSET, { status: 201 })
      }),
    )
    const asset = await uploadPrivateImage(new File(['x'], 'shot.png', { type: 'image/png' }))
    expect(asset.key).toBe(ASSET.key)
    expect(contentType).toMatch(/^multipart\/form-data/)
    expect(rawBody).toContain('name="is_private"')
    expect(rawBody).toContain('name="file"')
    expect(rawBody).toContain('Content-Type: image/png')
  })
})

describe('fetchImageBlob', () => {
  it('mints a signed URL for the key, then fetches it with the bearer header', async () => {
    tokenStore.set('tok-123')
    let mintedKey: string | null = null
    let fetchAuth: string | null = null
    server.use(
      http.get('http://localhost/api/v1/images/signed-url', ({ request }) => {
        mintedKey = new URL(request.url).searchParams.get('key')
        return HttpResponse.json({
          url: '/api/v1/images/signed/tok.abc',
          expires_at: '2026-07-17T01:00:00Z',
        })
      }),
      http.get('http://localhost/api/v1/images/signed/tok.abc', ({ request }) => {
        fetchAuth = request.headers.get('Authorization')
        return new HttpResponse('png-bytes', { headers: { 'Content-Type': 'image/png' } })
      }),
    )
    const blob = await fetchImageBlob(ASSET.key)
    expect(mintedKey).toBe(ASSET.key)
    expect(fetchAuth).toBe('Bearer tok-123')
    expect(await blob.text()).toBe('png-bytes')
  })

  it('throws when the signed fetch fails (e.g. the blob was reaped)', async () => {
    server.use(
      http.get('http://localhost/api/v1/images/signed-url', () =>
        HttpResponse.json({
          url: '/api/v1/images/signed/tok.gone',
          expires_at: '2026-07-17T01:00:00Z',
        }),
      ),
      http.get('http://localhost/api/v1/images/signed/tok.gone', () =>
        HttpResponse.json({ title: 'Not Found' }, { status: 404 }),
      ),
    )
    // The status is a field on the thrown `ApiError` now, not text in its message — that is what
    // lets a consent refusal be told apart from a genuine failure downstream.
    await expect(fetchImageBlob(ASSET.key)).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
    })
  })
})
