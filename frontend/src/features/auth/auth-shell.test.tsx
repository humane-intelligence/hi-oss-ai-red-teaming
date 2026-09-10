import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { AuthShell } from './auth-shell'

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

  it('names the mobile readout as a complementary landmark', () => {
    renderShell()
    // jsdom loads no stylesheet, so this asserts the markup and its landmark semantics, not the
    // breakpoint: CSS (`hidden lg:flex` / `lg:hidden`) is what shows exactly one copy per viewport,
    // and `display: none` keeps the other out of the accessibility tree.
    // Named, not counted: `getAllByRole('complementary')` matches a bare <aside> too, so a count
    // would stay green if the label went away — and it would also pin the decorative desktop panel
    // as a landmark, which this test has no opinion about.
    const mobile = screen.getByRole('complementary', { name: 'console status' })
    expect(within(mobile).getByText('operational')).toBeInTheDocument()
    expect(within(mobile).getByText('unauthenticated')).toBeInTheDocument()
  })

  it('keeps the status dot out of the accessibility tree', () => {
    renderShell()
    // The state is in the adjacent text; announcing the glyph would prefix it with "black circle".
    for (const dot of screen.getAllByText('●')) expect(dot).toHaveAttribute('aria-hidden', 'true')
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
  })

  it('drops only the version row when /version fails', async () => {
    server.use(http.get('http://localhost/version', () => HttpResponse.json({}, { status: 500 })))
    const qc = renderShell()
    // The query settles into an error state; waiting on that is the render-side signal.
    await waitFor(() => expect(qc.getQueryState(['version'])?.status).toBe('error'))
    expect(screen.queryAllByText('version')).toHaveLength(0)
    // The rest of the readout does not depend on /version.
    expect(screen.getAllByText('operational')).toHaveLength(2)
    expect(screen.getAllByText('unauthenticated')).toHaveLength(2)
  })
})
