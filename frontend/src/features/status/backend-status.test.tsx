import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { authWrapper, role } from '@/lib/auth/auth.testutils'
import { BackendStatusPage } from './backend-status'

function readyHandler(body: object, status = 200) {
  return http.get('http://localhost/ready', () => HttpResponse.json(body, { status }))
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper([], [role('admin')])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <BackendStatusPage />
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('BackendStatusPage', () => {
  it('shows Operational banner and per-check rows when status is ok', async () => {
    server.use(
      readyHandler({
        status: 'ok',
        checks: {
          database: { ok: true },
          redis: { ok: false, error: 'timeout' },
        },
      }),
    )

    renderPage()

    await waitFor(() => expect(screen.getByText('Operational')).toBeInTheDocument())
    expect(screen.getByText('database')).toBeInTheDocument()
    expect(screen.getByText('redis')).toBeInTheDocument()
    expect(screen.getByText('timeout')).toBeInTheDocument()
    // Per-check status is conveyed to screen readers (the dot is aria-hidden), not by colour alone.
    expect(screen.getByText('ok')).toBeInTheDocument() // database
    expect(screen.getByText('failing')).toBeInTheDocument() // redis
    // The live "heartbeat" pulse is only on healthy checks; a failing dot stays solid.
    expect(document.querySelector('.bg-ok')?.className).toContain('animate-pulse')
    expect(document.querySelector('.bg-err')?.className).not.toContain('animate-pulse')
  })

  it('shows Unavailable banner when status is unavailable (503 with body)', async () => {
    server.use(
      readyHandler(
        {
          status: 'unavailable',
          checks: {
            database: { ok: false, error: 'connection refused' },
          },
        },
        503,
      ),
    )

    renderPage()

    await waitFor(() => expect(screen.getByText('Unavailable')).toBeInTheDocument())
    expect(screen.getByText('database')).toBeInTheDocument()
    expect(screen.getByText('connection refused')).toBeInTheDocument()
  })

  it('shows the backend version and environment', async () => {
    server.use(
      readyHandler({ status: 'ok', checks: {} }),
      http.get('http://localhost/version', () =>
        HttpResponse.json({ version: 'a4933be', environment: 'dev' }),
      ),
    )

    renderPage()

    await waitFor(() => expect(screen.getByText('a4933be')).toBeInTheDocument())
    expect(screen.getByText(/dev/)).toBeInTheDocument()
  })

  it('shows Backend unreachable on network-level error', async () => {
    server.use(http.get('http://localhost/ready', () => HttpResponse.error()))

    renderPage()

    await waitFor(() => expect(screen.getByText(/backend unreachable/i)).toBeInTheDocument())
  })
})
