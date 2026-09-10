import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useUpdatePlatformSettings } from './mutations'

// Real keys as the feature hooks spell them, not the mutation's own list — a prefix that misses
// (['evaluation'] against the ['evaluations', params] list, say) has to fail here.
const SEEDED_KEYS = [
  ['licenses'],
  ['license', 'lic-1'],
  ['evaluation', 'ev-1'],
  ['evaluations', { limit: 20, offset: 0 }],
  ['evaluation-group', 'grp-1'],
  ['evaluation-groups', { limit: 20, offset: 0 }],
  ['conversation', 'conv-1'],
  ['conversations', 'ev-1'],
  ['conversation-group', 'cg-1'],
  ['conversation-groups', 'all', {}],
]

describe('platform-settings update invalidates every license-derived read', () => {
  it('marks the license catalog and every effective_license carrier stale', async () => {
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', () =>
        HttpResponse.json({
          default_license_id: 'lic-0001-0000-0000-000000000000',
          invite_only: false,
          updated_at: '2026-08-12T10:00:00Z',
        }),
      ),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    for (const key of SEEDED_KEYS) qc.setQueryData(key, { seeded: true })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useUpdatePlatformSettings(), { wrapper })

    await act(() => result.current.mutateAsync({ invite_only: false }))

    for (const key of SEEDED_KEYS) {
      expect(qc.getQueryState(key)?.isInvalidated, key.join('/')).toBe(true)
    }
  })

  it('leaves reads that do not resolve the singleton alone', async () => {
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', () =>
        HttpResponse.json({
          default_license_id: 'lic-0001-0000-0000-000000000000',
          invite_only: true,
          updated_at: '2026-08-12T10:00:00Z',
        }),
      ),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['users', { limit: 20 }], { seeded: true })
    qc.setQueryData(['evaluation-group-members', 'grp-1'], { seeded: true })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useUpdatePlatformSettings(), { wrapper })

    await act(() => result.current.mutateAsync({ invite_only: true }))

    expect(qc.getQueryState(['users', { limit: 20 }])?.isInvalidated).toBe(false)
    // The member list shares a prefix word with ['evaluation-group'] but not its first element.
    expect(qc.getQueryState(['evaluation-group-members', 'grp-1'])?.isInvalidated).toBe(false)
  })
})
