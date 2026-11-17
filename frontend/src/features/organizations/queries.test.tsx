import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { ALL_MEMBERS_CAP, useAllOrganizationMembers } from './queries'

const ORG_ID = 'org-0001-0000-0000-000000000000'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

// Serves `total` members, 100 a page, and records the offsets asked for.
function serveMembers(total: number) {
  const offsets: number[] = []
  server.use(
    http.get(`http://localhost/api/v1/organizations/${ORG_ID}/members`, ({ request }) => {
      const url = new URL(request.url)
      const offset = Number(url.searchParams.get('offset'))
      const limit = Number(url.searchParams.get('limit'))
      offsets.push(offset)
      const count = Math.max(0, Math.min(limit, total - offset))
      const items = Array.from({ length: count }, (_, i) => ({ id: `u${offset + i}` }))
      return HttpResponse.json({ items, total, limit, offset })
    }),
  )
  return offsets
}

describe('useAllOrganizationMembers', () => {
  it('walks every page, so an organization over 100 is not cut short', async () => {
    const offsets = serveMembers(250)
    const { result } = renderHook(() => useAllOrganizationMembers(ORG_ID), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.items).toHaveLength(250)
    expect(offsets).toEqual([0, 100, 200])
  })

  it('stops at the bulk row cap and still reports the real total', async () => {
    serveMembers(ALL_MEMBERS_CAP + 50)
    const { result } = renderHook(() => useAllOrganizationMembers(ORG_ID), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.items).toHaveLength(ALL_MEMBERS_CAP)
    expect(result.current.data?.total).toBe(ALL_MEMBERS_CAP + 50)
  })
})
