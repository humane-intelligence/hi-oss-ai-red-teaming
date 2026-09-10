import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useUpdateRole } from './mutations'

const ROLE_ID = 'role-0001-0000-0000-000000000000'
const OTHER_ROLE_ID = 'role-0002-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { spy, wrapper }
}

describe('role mutations refresh the embedded-role caches', () => {
  it('invalidates users and members after a role update', async () => {
    server.use(
      http.patch(`http://localhost/api/v1/roles/${ROLE_ID}`, () =>
        HttpResponse.json({
          id: ROLE_ID,
          name: 'lead_reviewer',
          display_name: 'Lead Reviewer',
          permissions: [],
          is_system: false,
          is_active: false,
          is_default: false,
          is_participant_default: false,
        }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useUpdateRole(ROLE_ID), { wrapper })

    await act(() => result.current.mutateAsync({ is_active: false }))

    for (const queryKey of [
      ['roles'],
      ['role'],
      ['users'],
      ['user'],
      ['evaluation-group-members'],
    ]) {
      await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey })))
    }
  })

  it('marks another role entity stale — a new default clears the previous holder server-side', async () => {
    server.use(
      http.patch(`http://localhost/api/v1/roles/${ROLE_ID}`, () =>
        HttpResponse.json({
          id: ROLE_ID,
          name: 'lead_reviewer',
          display_name: 'Lead Reviewer',
          permissions: [],
          is_system: false,
          is_active: true,
          is_default: true,
          is_participant_default: false,
          is_object_assignable: false,
        }),
      ),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    // The role that holds the default today, cached from a visit to its edit form.
    qc.setQueryData(['role', OTHER_ROLE_ID], { id: OTHER_ROLE_ID, is_default: true })
    const { result } = renderHook(() => useUpdateRole(ROLE_ID), { wrapper })

    await act(() => result.current.mutateAsync({ is_default: true }))

    await waitFor(() => expect(qc.getQueryState(['role', OTHER_ROLE_ID])?.isInvalidated).toBe(true))
  })
})
