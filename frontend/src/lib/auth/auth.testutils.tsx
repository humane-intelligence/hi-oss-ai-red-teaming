import type { ReactNode } from 'react'
import type { MeResponse, OrganizationBase, RoleSummary } from '@/lib/api/types'
import { ExportsProvider } from '@/features/exports/exports-store'
import { AuthContext, type AuthContextValue } from './auth-context'

// The id `authWrapper` authenticates as. Exported so a fixture can state that the caller owns
// the row under test — owner-scoped affordances compare against this.
export const AUTH_USER_ID = '1'

// `overrides` carries both halves of the context a test may need to steer: `user` fields
// (e.g. `has_password`) and the callbacks (a spied `logout`, for a flow that ends the session).
type AuthOverrides = Partial<Omit<AuthContextValue, 'status' | 'user'>> & {
  user?: Partial<MeResponse>
}

export function authWrapper(
  permissions: string[],
  roles: RoleSummary[] = [],
  organization: OrganizationBase | null = null,
  { user: userOverrides, ...callbacks }: AuthOverrides = {},
) {
  const user: MeResponse = {
    id: AUTH_USER_ID,
    email: 'a@b.c',
    provider: 'local',
    email_verified: true,
    has_password: true,
    roles,
    permissions,
    organization,
    consent_terms: false,
    consent_emails: false,
    terms_accepted_at: null,
    terms_acceptance_required: false,
    ...userOverrides,
  }
  const value: AuthContextValue = {
    status: 'authenticated',
    user,
    login: async () => {},
    adoptSession: async () => {},
    logout: () => {},
    updateUser: () => {},
    ...callbacks,
  }
  return ({ children }: { children: ReactNode }) => (
    <AuthContext.Provider value={value}>
      <ExportsProvider>{children}</ExportsProvider>
    </AuthContext.Provider>
  )
}

export function role(name: string): RoleSummary {
  return { id: name, name, display_name: name }
}
