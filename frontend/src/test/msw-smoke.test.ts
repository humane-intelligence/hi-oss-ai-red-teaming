import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from './msw/server'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'

describe('msw + apiClient + unwrap', () => {
  it('returns mocked provider list', async () => {
    // jsdom origin is http://localhost; apiClient uses VITE_API_BASE_URL as base
    server.use(
      http.get('http://localhost/api/v1/auth/oidc/providers', () => HttpResponse.json(['google'])),
    )
    const data = unwrap(await apiClient.GET('/api/v1/auth/oidc/providers'))
    expect(data).toEqual(['google'])
  })
})
