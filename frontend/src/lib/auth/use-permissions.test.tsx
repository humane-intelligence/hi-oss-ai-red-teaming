import { describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { usePermissions } from './use-permissions'
import { AuthContext, type AuthContextValue } from './auth-context'
import { authWrapper, role } from './auth.testutils'

describe('usePermissions', () => {
  it('has() / hasAny() / hasAll() reflect the permission set', () => {
    const { result } = renderHook(() => usePermissions(), {
      wrapper: authWrapper(['conversations:create', 'flags:create']),
    })
    expect(result.current.has('conversations:create')).toBe(true)
    expect(result.current.has('users:delete')).toBe(false)
    expect(result.current.hasAny(['users:delete', 'flags:create'])).toBe(true)
    expect(result.current.hasAll(['conversations:create', 'flags:create'])).toBe(true)
    expect(result.current.hasAll(['conversations:create', 'users:delete'])).toBe(false)
  })

  it('hasRole() reflects the role set', () => {
    const { result } = renderHook(() => usePermissions(), {
      wrapper: authWrapper([], [role('admin')]),
    })
    expect(result.current.hasRole('admin')).toBe(true)
    expect(result.current.hasRole('reviewer')).toBe(false)
  })

  it('hasResource() matches by permission prefix', () => {
    const { result } = renderHook(() => usePermissions(), {
      wrapper: authWrapper(['reviews:update', 'flags:create']),
    })
    expect(result.current.hasResource('reviews')).toBe(true)
    expect(result.current.hasResource('flags')).toBe(true)
    expect(result.current.hasResource('users')).toBe(false)
  })

  it('is empty for an unauthenticated user', () => {
    const value: AuthContextValue = {
      status: 'unauthenticated',
      user: null,
      login: async () => {},
      adoptSession: async () => {},
      logout: () => {},
      updateUser: () => {},
    }
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
    )
    const { result } = renderHook(() => usePermissions(), { wrapper })
    expect(result.current.permissions).toEqual([])
    expect(result.current.has('anything')).toBe(false)
  })
})
