import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/msw/server'
import { tokenStore } from '@/lib/auth/token-store'
import {
  fetchImageBlob,
  publicImageUrl,
  uploadPrivateImage,
  uploadPublicImage,
  validateImageFile,
} from './images'

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
    // Asserted on the FormData: jsdom's Blob has no stream(), so the wire body cannot be read here.
    const append = vi.spyOn(FormData.prototype, 'append')
    onTestFinished(() => append.mockRestore())
    let contentType: string | null = null
    server.use(
      http.post('http://localhost/api/v1/images', ({ request }) => {
        contentType = request.headers.get('Content-Type')
        return HttpResponse.json(ASSET, { status: 201 })
      }),
    )
    const file = new File(['x'], 'shot.png', { type: 'image/png' })
    const asset = await uploadPrivateImage(file)
    expect(asset.key).toBe(ASSET.key)
    expect(contentType).toMatch(/^multipart\/form-data/)
    expect(append.mock.calls).toEqual([
      ['file', file],
      ['is_private', 'true'],
    ])
  })
})

describe('uploadPublicImage', () => {
  it('posts is_private=false', async () => {
    const append = vi.spyOn(FormData.prototype, 'append')
    onTestFinished(() => append.mockRestore())
    server.use(
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json({ ...ASSET, is_private: false }, { status: 201 }),
      ),
    )
    const file = new File(['x'], 'cover.png', { type: 'image/png' })
    const asset = await uploadPublicImage(file)
    expect(asset.key).toBe(ASSET.key)
    expect(append.mock.calls).toEqual([
      ['file', file],
      ['is_private', 'false'],
    ])
  })
})

describe('publicImageUrl', () => {
  it('points at the public GET with the key as a path', () => {
    expect(publicImageUrl(ASSET.key)).toBe(`http://localhost${ASSET.url}`)
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
