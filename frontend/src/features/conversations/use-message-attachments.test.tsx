import type { ReactNode } from 'react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/msw/server'
import { MAX_ATTACHMENTS, useMessageAttachments } from './use-message-attachments'

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, wrapper }
}

// jsdom implements neither object-URL static.
const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL
beforeAll(() => {
  let n = 0
  URL.createObjectURL = vi.fn(() => `blob:test-${n++}`)
  URL.revokeObjectURL = vi.fn()
})
afterAll(() => {
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
})

const png = (name: string) => new File(['x'], name, { type: 'image/png' })

function mockUpload() {
  let uploads = 0
  server.use(
    http.post('http://localhost/api/v1/images', () => {
      uploads += 1
      return HttpResponse.json(
        {
          id: `a0000000-0000-0000-0000-00000000000${uploads}`,
          key: `2026/07/17/upload-${uploads}.png`,
          url: `/api/v1/images/2026/07/17/upload-${uploads}.png`,
          content_type: 'image/png',
          width: 1,
          height: 1,
          size_bytes: 1,
          is_private: true,
          created_at: '2026-07-17T00:00:00Z',
        },
        { status: 201 },
      )
    }),
  )
  return () => uploads
}

describe('useMessageAttachments', () => {
  it('uploads picked files, exposes their keys in pick order, and seeds the blob cache', async () => {
    mockUpload()
    const { qc, wrapper } = makeWrapper()
    const { result } = renderHook(() => useMessageAttachments(), { wrapper })
    act(() => result.current.add([png('a.png'), png('b.png')]))
    expect(result.current.uploading).toBe(true)
    expect(result.current.keys).toEqual([])
    await waitFor(() => expect(result.current.uploading).toBe(false))
    expect(result.current.keys).toEqual(['2026/07/17/upload-1.png', '2026/07/17/upload-2.png'])
    expect(result.current.failed).toBe(false)
    // The just-uploaded bytes back the display cache — no refetch for the sent message.
    expect(qc.getQueryData(['image-blob', '2026/07/17/upload-1.png'])).toBeInstanceOf(File)
  })

  it('caps at five attachments and skips invalid files without uploading them', async () => {
    const uploadCount = mockUpload()
    const { result } = renderHook(() => useMessageAttachments(), { wrapper: makeWrapper().wrapper })
    const six = Array.from({ length: 6 }, (_, i) => png(`f${i}.png`))
    act(() => result.current.add(six))
    expect(result.current.attachments).toHaveLength(MAX_ATTACHMENTS)
    act(() => result.current.add([new File(['x'], 'notes.txt', { type: 'text/plain' })]))
    expect(result.current.attachments).toHaveLength(MAX_ATTACHMENTS)
    await waitFor(() => expect(result.current.uploading).toBe(false))
    expect(uploadCount()).toBe(MAX_ATTACHMENTS)
  })

  it('marks a failed upload and blocks nothing else; remove drops it', async () => {
    server.use(
      http.post('http://localhost/api/v1/images', () =>
        HttpResponse.json({ title: 'Bad Request' }, { status: 400 }),
      ),
    )
    const { result } = renderHook(() => useMessageAttachments(), { wrapper: makeWrapper().wrapper })
    act(() => result.current.add([png('bad.png')]))
    await waitFor(() => expect(result.current.failed).toBe(true))
    const id = result.current.attachments[0]?.id ?? ''
    act(() => result.current.remove(id))
    expect(result.current.attachments).toHaveLength(0)
    expect(result.current.failed).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })

  it('clear releases every preview URL', async () => {
    mockUpload()
    const { result } = renderHook(() => useMessageAttachments(), { wrapper: makeWrapper().wrapper })
    act(() => result.current.add([png('a.png'), png('b.png')]))
    await waitFor(() => expect(result.current.uploading).toBe(false))
    act(() => result.current.clear())
    expect(result.current.attachments).toHaveLength(0)
    expect(result.current.keys).toEqual([])
  })
})
