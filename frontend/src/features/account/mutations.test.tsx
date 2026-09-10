import { describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { useUpdateMe } from './mutations'
import type { MeResponse } from '@/lib/api/types'

const ME_ID = '1'

function withClient(updateUser: (me: MeResponse) => void = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const Auth = authWrapper([], [], null, { updateUser })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Auth>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Auth>
  )
  return { spy, wrapper }
}

function mockPatch() {
  server.use(
    http.patch('http://localhost/api/v1/auth/me', () =>
      HttpResponse.json({
        id: ME_ID,
        email: 'a@b.c',
        provider: 'local',
        email_verified: true,
        has_password: true,
        first_name: 'Augusta',
        last_name: null,
        roles: [],
        permissions: [],
        organization: null,
      }),
    ),
  )
}

describe('useUpdateMe', () => {
  // The invalidation set is inherited from `features/users` — pinned from this side too, so a
  // tweak made for the delete/restore use-case surfaces as a self-rename regression.
  it('invalidates every cache that projects the caller identity', async () => {
    mockPatch()
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useUpdateMe(), { wrapper })

    await act(() => result.current.mutateAsync({ first_name: 'Augusta' }))

    await waitFor(() => {
      for (const queryKey of [
        ['users'],
        ['user', ME_ID],
        ['evaluation-group-members'],
        ['organization-members'],
      ]) {
        expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey }))
      }
    })
  })

  it('adopts the response into the held identity', async () => {
    mockPatch()
    const updateUser = vi.fn()
    const { wrapper } = withClient(updateUser)
    const { result } = renderHook(() => useUpdateMe(), { wrapper })

    await act(() => result.current.mutateAsync({ first_name: 'Augusta' }))

    await waitFor(() =>
      expect(updateUser).toHaveBeenCalledWith(expect.objectContaining({ first_name: 'Augusta' })),
    )
  })
})
