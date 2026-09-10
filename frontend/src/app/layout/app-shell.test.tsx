import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AppShell } from './app-shell'
import { server } from '@/test/msw/server'
import { authWrapper, role } from '@/lib/auth/auth.testutils'
import { ExportsContext } from '@/features/exports/exports-context'
import type { RoleSummary } from '@/lib/api/types'

// jsdom implements no window.matchMedia. The stub keeps its listeners so a test can drive the
// breakpoint crossing via widenToDesktop(). (The <dialog> stubs live in src/test/setup.ts.)
const mediaListeners = new Set<() => void>()
let matchesDesktop = false

beforeAll(() => {
  window.matchMedia = ((media: string) => ({
    media,
    get matches() {
      return matchesDesktop
    },
    addEventListener: (_: string, fn: () => void) => void mediaListeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => void mediaListeners.delete(fn),
  })) as typeof window.matchMedia
})

afterEach(() => {
  mediaListeners.clear()
  matchesDesktop = false
})

function widenToDesktop() {
  matchesDesktop = true
  act(() => mediaListeners.forEach((fn) => fn()))
}

function renderShell(permissions: string[], roles: RoleSummary[] = []) {
  const Wrapper = authWrapper(permissions, roles)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Wrapper>
        <MemoryRouter>
          <AppShell />
        </MemoryRouter>
      </Wrapper>
    </QueryClientProvider>,
  )
}

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

describe('AppShell nav gating', () => {
  it('shows only sections the user has permission for (ungated admin items hidden without admin role)', () => {
    renderShell(['reviews:read'])
    expect(screen.getByRole('link', { name: /^reviews$/i })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /review queue/i })).toBeNull() // merged into Reviews tab
    expect(screen.queryByRole('link', { name: /backend status/i })).toBeNull() // needs admin role
    expect(screen.queryByRole('link', { name: /ai models/i })).toBeNull() // needs models:read
    expect(screen.queryByRole('link', { name: /^evaluations$/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /users/i })).toBeNull()
    // Ungated on purpose: every authenticated caller owns their own account, and this is
    // the only entry point below `md`, where the header's email link is hidden. Exact name —
    // the header link's own accessible name is `Account settings (<email>)`.
    expect(screen.getByRole('link', { name: 'Account settings' })).toBeInTheDocument()
  })

  it('shows Users for a user with users:read permission', () => {
    renderShell(['users:read'])
    expect(screen.getByRole('link', { name: /users/i })).toBeInTheDocument()
  })

  it('gates the standalone Evaluations item on evaluations:read, not on group access', () => {
    renderShell(['evaluation_groups:read'])
    expect(screen.getByRole('link', { name: /evaluation groups/i })).toBeInTheDocument()
    // Group access alone does not open the all-evaluations list.
    expect(screen.queryByRole('link', { name: /^evaluations$/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /scenarios/i })).toBeNull()
  })

  it('shows the standalone Evaluations item for evaluations:read', () => {
    renderShell(['evaluations:read'])
    expect(screen.getByRole('link', { name: /^evaluations$/i })).toHaveAttribute(
      'href',
      '/evaluations',
    )
  })

  it('shows AI Models only for a user with models:read', () => {
    renderShell(['models:read'])
    expect(screen.getByRole('link', { name: /ai models/i })).toBeInTheDocument()
  })

  it('red-teamer sees Evaluation Groups + Evaluations + My flags + Reviews (read-only) but NOT Backend status, AI Models, Users', () => {
    renderShell(redTeamerPerms)
    expect(screen.getByRole('link', { name: /evaluation groups/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^evaluations$/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /my flags/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^reviews$/i })).toBeInTheDocument() // reviews:read now opens Reviews (read-only)
    expect(screen.queryByRole('link', { name: /backend status/i })).toBeNull() // needs admin role
    expect(screen.queryByRole('link', { name: /scenarios/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /ai models/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /users/i })).toBeNull()
  })

  it('admin with all permissions and admin role sees all nav items including Backend status', () => {
    renderShell(
      [
        'evaluations:read',
        'evaluations:update',
        'evaluations:create',
        'evaluation_groups:read',
        'flags:read',
        'reviews:read',
        'reviews:update',
        'models:read',
        'users:read',
      ],
      [role('admin')],
    )
    expect(screen.getByRole('link', { name: /evaluation groups/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^evaluations$/i })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /scenarios/i })).toBeNull()
    expect(screen.getByRole('link', { name: /my flags/i })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /review queue/i })).toBeNull() // merged into Reviews tab
    expect(screen.getByRole('link', { name: /^reviews$/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /ai models/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /users/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /backend status/i })).toBeInTheDocument()
  })

  it('shows Roles for roles:read and hides it without', () => {
    renderShell(['roles:read'])
    expect(screen.getByRole('link', { name: /^roles$/i })).toBeInTheDocument()
  })

  it('hides Roles without roles:read', () => {
    renderShell(['users:read'])
    expect(screen.queryByRole('link', { name: /^roles$/i })).toBeNull()
  })

  it('shows System Preferences for platform_settings:read', () => {
    renderShell(['platform_settings:read'])
    expect(screen.getByRole('link', { name: /system preferences/i })).toBeInTheDocument()
  })

  it('hides System Preferences without platform_settings:read', () => {
    renderShell(['users:read'])
    expect(screen.queryByRole('link', { name: /system preferences/i })).toBeNull()
  })

  it('Backend status hidden for non-admin red_teamer role', () => {
    renderShell(redTeamerPerms, [role('red_teamer')])
    expect(screen.queryByRole('link', { name: /backend status/i })).toBeNull()
  })

  it('Backend status visible for admin role', () => {
    renderShell([], [role('admin')])
    expect(screen.getByRole('link', { name: /backend status/i })).toBeInTheDocument()
  })

  it('shows the notification bell with notifications:read', () => {
    server.use(
      http.get('http://localhost/api/v1/notifications', () =>
        HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 }),
      ),
    )
    renderShell(['notifications:read'])
    expect(screen.getByRole('button', { name: /notifications/i })).toBeInTheDocument()
  })

  it('hides the notification bell without notifications:read', () => {
    renderShell(['reviews:read'])
    expect(screen.queryByRole('button', { name: /notifications/i })).toBeNull()
  })

  it('opens the mobile nav drawer from the hamburger and closes it when a link is followed', async () => {
    const user = userEvent.setup()
    renderShell(['users:read'])
    // The persistent (desktop) sidebar renders the nav once; the drawer is closed, so no duplicate yet.
    expect(screen.getAllByRole('link', { name: /users/i })).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: /open navigation/i }))

    const drawer = await screen.findByRole('dialog')
    const drawerLink = within(drawer).getByRole('link', { name: /users/i })
    await user.click(drawerLink)

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('closes the nav drawer once the viewport is wide enough for the persistent sidebar', async () => {
    const user = userEvent.setup()
    renderShell(['users:read'])

    await user.click(screen.getByRole('button', { name: /open navigation/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    widenToDesktop()

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('hands focus to the sidebar nav when the breakpoint closes the drawer', async () => {
    const user = userEvent.setup()
    renderShell(['users:read'])

    await user.click(screen.getByRole('button', { name: /open navigation/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    widenToDesktop()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    // Asserts the handoff, not its ordering against close(): the stub above traps no focus.
    expect(screen.getByRole('link', { name: /^overview$/i })).toHaveFocus()
  })

  it('closes the nav drawer from its own close button', async () => {
    const user = userEvent.setup()
    renderShell(['users:read'])

    await user.click(screen.getByRole('button', { name: /open navigation/i }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: /close/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  // Injecting the context keeps this about the header; the polling is exports-store.test.tsx's job.
  // What jsdom can decide: the pill is a polite live region and the wording is rendered rather than
  // dropped at narrow widths. What it cannot: whether `sr-only` keeps it in the accessibility tree —
  // no stylesheet is loaded, so `hidden sm:inline` would pass this too. That half is a browser check
  // (`.sr-only` resolves to a clip, not `display: none`).
  function renderShellWithExports(activeCount: number) {
    const Wrapper = authWrapper(['users:read'])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <Wrapper>
          <ExportsContext.Provider value={{ activeCount, trackExport: () => {} }}>
            <MemoryRouter>
              <AppShell />
            </MemoryRouter>
          </ExportsContext.Provider>
        </Wrapper>
      </QueryClientProvider>,
    )
  }

  it('renders the export progress as a polite live region with the count in the DOM', () => {
    renderShellWithExports(2)

    const status = within(screen.getByRole('banner')).getByRole('status')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toHaveTextContent('Exporting 2…')
  })

  it('renders no export live region when nothing is in flight', () => {
    renderShellWithExports(0)

    expect(within(screen.getByRole('banner')).queryByRole('status')).toBeNull()
  })
})
