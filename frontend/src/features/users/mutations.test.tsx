import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import {
  useBulkChangeUserStatus,
  useBulkForceLogout,
  useBulkSendPasswordReset,
  useChangeUserStatus,
  useForceLogout,
  useResendInvitation,
  useRevokeInvitation,
  useSendPasswordReset,
} from './mutations'

const USER_ID = 'user-0001-0000-0000-000000000000'
const OTHER_ID = 'user-0002-0000-0000-000000000000'

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, spy, wrapper }
}

describe('force-logout mutations refresh the users cache', () => {
  it('invalidates users after a single force-logout (204)', async () => {
    server.use(
      http.post(
        `http://localhost/api/v1/auth/users/${USER_ID}/force-logout`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useForceLogout(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] })),
    )
  })

  it('invalidates users after a bulk force-logout', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/users/force-logout', () =>
        HttpResponse.json({
          dry_run: false,
          total: 1,
          succeeded: 1,
          failed: 0,
          results: [{ row_key: 'r1', status: 'ok', data: { user_id: USER_ID }, error: null }],
        }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useBulkForceLogout(), { wrapper })

    await act(() =>
      result.current.mutateAsync({
        dry_run: false,
        rows: [{ row_key: 'r1', data: { user_id: USER_ID } }],
      }),
    )

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] })),
    )
  })
})

describe('account status mutations refresh the users cache', () => {
  it('invalidates the row and the list after a single status change', async () => {
    server.use(
      http.post(`http://localhost/api/v1/auth/users/${USER_ID}/status`, async ({ request }) => {
        expect(await request.json()).toEqual({ status: 'inactive' })
        return HttpResponse.json({ id: USER_ID, email: 'carol@example.com', status: 'inactive' })
      }),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useChangeUserStatus(), { wrapper })

    await act(() => result.current.mutateAsync({ id: USER_ID, status: 'inactive' }))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] }))
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['user', USER_ID] }))
    })
  })

  it('invalidates the list and every row after a bulk status change, and surfaces the failed row', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/users/status', () =>
        HttpResponse.json({
          dry_run: false,
          total: 2,
          succeeded: 1,
          failed: 1,
          results: [
            {
              row_key: USER_ID,
              status: 'ok',
              data: { user_id: USER_ID, status: 'inactive' },
              error: null,
            },
            {
              row_key: OTHER_ID,
              status: 'failed',
              data: null,
              error: { title: 'Conflict', detail: 'Account is still onboarding.' },
            },
          ],
        }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useBulkChangeUserStatus(), { wrapper })

    const data = await act(() =>
      result.current.mutateAsync({
        dry_run: false,
        rows: [
          { row_key: USER_ID, data: { user_id: USER_ID, status: 'inactive' } },
          { row_key: OTHER_ID, data: { user_id: OTHER_ID, status: 'inactive' } },
        ],
      }),
    )

    expect(data).toMatchObject({ succeeded: 1, failed: 1 })
    expect(data.results[1]).toMatchObject({
      row_key: OTHER_ID,
      status: 'failed',
      error: { detail: 'Account is still onboarding.' },
    })
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] })),
    )
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['user', USER_ID] }))
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['user', OTHER_ID] }))
  })
})

describe('password reset mutations leave the users cache alone', () => {
  it('does not invalidate after a single reset — no field on the row changes', async () => {
    server.use(
      http.post(
        `http://localhost/api/v1/auth/users/${USER_ID}/password-reset`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useSendPasswordReset(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(spy).not.toHaveBeenCalled()
  })

  it('does not invalidate after a bulk reset and returns the envelope', async () => {
    server.use(
      http.post('http://localhost/api/v1/auth/users/password-reset', () =>
        HttpResponse.json({
          dry_run: false,
          total: 1,
          succeeded: 0,
          failed: 1,
          results: [
            {
              row_key: USER_ID,
              status: 'failed',
              data: null,
              error: { title: 'Conflict', detail: 'Account has no password to reset.' },
            },
          ],
        }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useBulkSendPasswordReset(), { wrapper })

    const data = await act(() =>
      result.current.mutateAsync({
        dry_run: false,
        rows: [{ row_key: USER_ID, data: { user_id: USER_ID } }],
      }),
    )

    expect(data).toMatchObject({ succeeded: 0, failed: 1 })
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('invitation admin actions refresh the users cache', () => {
  it('invalidates users after a resend (200)', async () => {
    server.use(
      http.post(`http://localhost/api/v1/auth/users/${USER_ID}/invitation/resend`, () =>
        HttpResponse.json({
          id: 'inv-0002-0000-0000-000000000000',
          user_id: USER_ID,
          email: 'carol@example.com',
          status: 'pending',
          expires_at: '2026-08-05T12:00:00Z',
          created_at: '2026-07-29T12:00:00Z',
        }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useResendInvitation(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] })),
    )
  })

  it('invalidates users after a revoke (204)', async () => {
    server.use(
      http.delete(
        `http://localhost/api/v1/auth/users/${USER_ID}/invitation`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const { spy, wrapper } = withClient()
    const { result } = renderHook(() => useRevokeInvitation(), { wrapper })

    await act(() => result.current.mutateAsync(USER_ID))

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['users'] })),
    )
  })
})
