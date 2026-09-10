import { createContext, useContext } from 'react'
import type { MeResponse } from '@/lib/api/types'

export type AuthState =
  | { status: 'loading'; user: null }
  | { status: 'authenticated'; user: MeResponse }
  | { status: 'unauthenticated'; user: null }

export type AuthContextValue = AuthState & {
  login: (email: string, password: string) => Promise<void>
  // Adopt a session minted elsewhere — the OIDC callback hands us the access
  // token in a URL fragment, so there are no credentials to post.
  adoptSession: (accessToken: string) => Promise<void>
  logout: () => void
  // Replace the held identity with a fresh MeResponse (e.g. the body a
  // self-service PATCH returns) — the identity lives in provider state, not a
  // query cache, so invalidation cannot refresh it.
  updateUser: (user: MeResponse) => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}
