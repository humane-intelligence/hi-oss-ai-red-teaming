import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/lib/auth/auth-context'
import { RequirePermission } from '@/lib/auth/require-permission'
import { RequireRole } from '@/lib/auth/require-role'
import { authWrapper, role } from '@/lib/auth/auth.testutils'
import { server } from '@/test/msw/server'
import type { RoleSummary } from '@/lib/api/types'
import { router } from './router'

// Red-teamer perms: conversations:* , evaluation_groups:read, evaluations:read, flags:* , reviews:read
const redTeamerPerms = [
  'conversations:create',
  'conversations:delete',
  'conversations:participate',
  'conversations:read',
  'conversations:update',
  'evaluation_groups:read',
  'evaluations:read',
  'flags:create',
  'flags:delete',
  'flags:read',
  'flags:update',
  'reviews:read',
]

function renderRoute(permissions: string[], path: string, anyOf: string[], content: string) {
  const Wrapper = authWrapper(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Wrapper>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path={path}
              element={
                <RequirePermission anyOf={anyOf}>
                  <div>{content}</div>
                </RequirePermission>
              }
            />
          </Routes>
        </MemoryRouter>
      </Wrapper>
    </QueryClientProvider>,
  )
}

function renderRoleRoute(
  permissions: string[],
  roles: RoleSummary[],
  path: string,
  anyOf: string[],
  content: string,
) {
  const Wrapper = authWrapper(permissions, roles)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Wrapper>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path={path}
              element={
                <RequireRole anyOf={anyOf}>
                  <div>{content}</div>
                </RequireRole>
              }
            />
          </Routes>
        </MemoryRouter>
      </Wrapper>
    </QueryClientProvider>,
  )
}

describe('route guards', () => {
  it('red-teamer hitting /ai-models gets NotAuthorized fallback (lacks models:read)', () => {
    renderRoute(redTeamerPerms, '/ai-models', ['models:read'], 'AI Models content')
    expect(screen.queryByText('AI Models content')).toBeNull()
    expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
  })

  it('red-teamer hitting /reviews/mine gets NotAuthorized fallback (has reviews:read, needs reviews:update)', () => {
    renderRoute(redTeamerPerms, '/reviews/mine', ['reviews:update'], 'My Reviews content')
    expect(screen.queryByText('My Reviews content')).toBeNull()
    expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
  })

  it('red-teamer can access /evaluations (has evaluations:read)', () => {
    renderRoute(redTeamerPerms, '/evaluations', ['evaluations:read'], 'Evaluations content')
    expect(screen.getByText('Evaluations content')).toBeInTheDocument()
    expect(screen.queryByText(/don't have access/i)).toBeNull()
  })

  it('user with models:read can access /ai-models', () => {
    renderRoute(['models:read'], '/ai-models', ['models:read'], 'AI Models content')
    expect(screen.getByText('AI Models content')).toBeInTheDocument()
    expect(screen.queryByText(/don't have access/i)).toBeNull()
  })

  it('a user without platform_settings:read gets NotAuthorized on /system-preferences', () => {
    renderRoute(
      redTeamerPerms,
      '/system-preferences',
      ['platform_settings:read'],
      'System Preferences content',
    )
    expect(screen.queryByText('System Preferences content')).toBeNull()
    expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
  })

  it('platform_settings:read alone reaches /system-preferences (update gates the form, not the route)', () => {
    renderRoute(
      ['platform_settings:read'],
      '/system-preferences',
      ['platform_settings:read'],
      'System Preferences content',
    )
    expect(screen.getByText('System Preferences content')).toBeInTheDocument()
    expect(screen.queryByText(/don't have access/i)).toBeNull()
  })

  describe('/status — admin-only via RequireRole', () => {
    it('non-admin hitting /status gets NotAuthorized fallback', () => {
      renderRoleRoute(redTeamerPerms, [role('red_teamer')], '/status', ['admin'], 'Status content')
      expect(screen.queryByText('Status content')).toBeNull()
      expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
    })

    it('admin reaching /status sees the page content', async () => {
      server.use(
        http.get('http://localhost/ready', () =>
          HttpResponse.json({ status: 'ok', checks: { database: { ok: true } } }),
        ),
      )
      renderRoleRoute([], [role('admin')], '/status', ['admin'], 'Status content')
      await waitFor(() => expect(screen.getByText('Status content')).toBeInTheDocument())
      expect(screen.queryByText(/don't have access/i)).toBeNull()
    })
  })
})

// `router` is a module-level singleton (`createBrowserRouter` binds to real DOM history) —
// a test added here that doesn't navigate first inherits whatever location the test before
// it left behind. Each such test must navigate to its own target before rendering.
describe('/auth/callback placement in the real router', () => {
  it('stays reachable while unauthenticated instead of redirecting to /login', async () => {
    // Against the actual exported `router`, not a hand-built `<Routes>` like the suite
    // above — a `RequireAuth`-nesting regression here drops the token in the URL
    // fragment before this page's own effect ever reads it, and a re-declared route
    // list can't catch that; only exercising the production tree can.
    const unauthenticated: AuthContextValue = {
      status: 'unauthenticated',
      user: null,
      login: async () => {},
      adoptSession: async () => {},
      logout: () => {},
      updateUser: () => {},
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await router.navigate('/auth/callback#error=access_denied')

    render(
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={unauthenticated}>
          <RouterProvider router={router} />
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(router.state.location.pathname).toBe('/auth/callback'))
    expect(screen.getByText(/sign-in failed/i)).toBeInTheDocument()
  })

  it('redirects an unauthenticated visit to /account to the login page', async () => {
    const unauthenticated: AuthContextValue = {
      status: 'unauthenticated',
      user: null,
      login: async () => {},
      adoptSession: async () => {},
      logout: () => {},
      updateUser: () => {},
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await router.navigate('/account')

    render(
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={unauthenticated}>
          <RouterProvider router={router} />
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
  })
})

describe('terms gate placement in the real router', () => {
  it('replaces a deep app route with the gate when an acceptance is owed', async () => {
    // Against the actual exported `router`: `terms-gate.test.tsx` builds its own two-route tree,
    // so a page declared as a sibling of `RequireTermsAcceptance` rather than under it would ship
    // un-gated with that suite green.
    server.use(
      http.get('http://localhost/api/v1/terms/current', () =>
        HttpResponse.json({
          id: 'terms-1',
          version: '1.0',
          content: 'The rules.',
          published_at: '2026-09-02T12:00:00Z',
        }),
      ),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(['evaluations:read'], [], null, {
      user: { terms_acceptance_required: true },
    })
    await router.navigate('/evaluations')

    render(
      <QueryClientProvider client={client}>
        <Wrapper>
          <RouterProvider router={router} />
        </Wrapper>
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Terms of service' })).toBeInTheDocument()
    // The route stays, so accepting lands where the user was going.
    expect(router.state.location.pathname).toBe('/evaluations')
  })
})
