import { describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { AuthShell } from './auth-shell'
import { publicSettingsHandler } from '@/test/msw/public-settings'

function versionHandler(environment: string) {
  let hit = false
  server.use(
    http.get('http://localhost/version', () => {
      hit = true
      return HttpResponse.json({ version: 'a4933be', environment })
    }),
  )
  return () => hit
}

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthShell title="Sign in" subtitle="Red Platform">
        <p>body</p>
      </AuthShell>
    </QueryClientProvider>,
  )
  return qc
}

describe('AuthShell', () => {
  it('renders title and children', () => {
    renderShell()
    expect(screen.getByText('Sign in')).toBeInTheDocument()
    expect(screen.getByText('body')).toBeInTheDocument()
  })

  it('shows the version off prod', async () => {
    versionHandler('dev')
    renderShell()
    // Two copies in the DOM (see the landmark test below), so pin the count — a third would be a
    // regression this assertion catches.
    await waitFor(() => expect(screen.getAllByText('a4933be')).toHaveLength(2))
    expect(screen.getAllByText('version')).toHaveLength(2)
  })

  it('names the mobile version note as a complementary landmark', async () => {
    versionHandler('dev')
    renderShell()
    // jsdom loads no stylesheet, so this asserts the markup and its landmark semantics, not the
    // breakpoint: CSS (`hidden lg:flex` / `lg:hidden`) is what shows exactly one copy per viewport,
    // and `display: none` keeps the other out of the accessibility tree.
    // Named, not counted: `getAllByRole('complementary')` matches a bare <aside> too, so a count
    // would stay green if the label went away — and it would also pin the decorative desktop panel
    // as a landmark, which this test has no opinion about.
    const mobile = await screen.findByRole('complementary', { name: 'console version' })
    expect(within(mobile).getByText('a4933be')).toBeInTheDocument()
  })

  it('shows no status or access readout', async () => {
    versionHandler('dev')
    renderShell()
    await waitFor(() => expect(screen.getAllByText('a4933be')).toHaveLength(2))
    expect(screen.queryByText('operational')).toBeNull()
    expect(screen.queryByText('unauthenticated')).toBeNull()
  })

  it('hides the version on prod', async () => {
    versionHandler('prod')
    const qc = renderShell()
    // Wait for the payload to land in the cache, not for the handler to have run: the resolver
    // flips its flag before the response is delivered, so the gate could "pass" pre-render.
    await waitFor(() =>
      expect(qc.getQueryData(['version'])).toEqual({ version: 'a4933be', environment: 'prod' }),
    )
    expect(screen.queryAllByText('version')).toHaveLength(0)
    expect(screen.queryAllByText('a4933be')).toHaveLength(0)
    expect(screen.queryByRole('complementary', { name: 'console version' })).toBeNull()
  })

  it('shows no version block when /version fails', async () => {
    server.use(http.get('http://localhost/version', () => HttpResponse.json({}, { status: 500 })))
    const qc = renderShell()
    // The query settles into an error state; waiting on that is the render-side signal.
    await waitFor(() => expect(qc.getQueryState(['version'])?.status).toBe('error'))
    expect(screen.queryAllByText('version')).toHaveLength(0)
    expect(screen.queryByRole('complementary', { name: 'console version' })).toBeNull()
  })
})

describe('AuthShell — client logo', () => {
  it('shows each theme its own logo above the headline', async () => {
    server.use(
      publicSettingsHandler({
        logo_light_key: '2026/11/17/light.png',
        logo_dark_key: '2026/11/17/dark.png',
      }),
    )
    renderShell()

    // `alt=""`: the heading names the page, so the logo is decorative and has no img role.
    const [light, dark] = await waitFor(() => {
      const logos = [...document.querySelectorAll('img[src*="/api/v1/images/"]')]
      expect(logos).toHaveLength(2)
      return logos
    })
    expect(light).toHaveAttribute('alt', '')
    expect(light).toHaveAttribute('src', 'http://localhost/api/v1/images/2026/11/17/light.png')
    expect(light).toHaveClass('dark:hidden')
    expect(dark).toHaveAttribute('src', 'http://localhost/api/v1/images/2026/11/17/dark.png')
    expect(dark).toHaveClass('hidden', 'dark:block')
  })

  it('holds the form invisible until the public settings settle', async () => {
    server.use(
      http.get('http://localhost/api/v1/platform-settings/public', async () => {
        await delay('infinite')
        return HttpResponse.json({})
      }),
    )
    renderShell()

    const column = screen.getByRole('heading', { name: 'Sign in' }).parentElement!.parentElement!
    expect(column).toHaveClass('invisible')
  })

  it('holds the form until both logo images have loaded', async () => {
    server.use(
      publicSettingsHandler({
        logo_light_key: '2026/11/17/light.png',
        logo_dark_key: '2026/11/17/dark.png',
      }),
    )
    renderShell()
    const column = screen.getByRole('heading', { name: 'Sign in' }).parentElement!.parentElement!

    const logos = await waitFor(() => {
      const found = [...document.querySelectorAll('img[src*="/api/v1/images/"]')]
      expect(found).toHaveLength(2)
      return found
    })
    expect(column).toHaveClass('invisible')
    fireEvent.load(logos[0]!)
    expect(column).toHaveClass('invisible')
    fireEvent.load(logos[1]!)

    await waitFor(() => expect(column).not.toHaveClass('invisible'))
  })

  it('shows the form after the deadline even if the settings never arrive', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      server.use(
        http.get('http://localhost/api/v1/platform-settings/public', async () => {
          await delay('infinite')
          return HttpResponse.json({})
        }),
      )
      renderShell()
      const column = screen.getByRole('heading', { name: 'Sign in' }).parentElement!.parentElement!
      expect(column).toHaveClass('invisible')

      act(() => vi.advanceTimersByTime(5_000))

      expect(column).not.toHaveClass('invisible')
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows no logo when none is set', async () => {
    renderShell()
    const column = screen.getByRole('heading', { name: 'Sign in' }).parentElement!.parentElement!
    await waitFor(() => expect(column).not.toHaveClass('invisible'))
    expect(document.querySelector('img[src*="/api/v1/images/"]')).toBeNull()
  })
})
