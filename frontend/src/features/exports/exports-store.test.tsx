import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ExportsProvider } from './exports-store'
import { useExports } from './exports-context'
import type { ExportJobStatus } from '@/lib/api/types'
import { ApiError, CONSENT_REQUIRED_TYPE } from '@/lib/api/problem'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

// Drive the polled job status directly — the store's watcher logic (toast on
// finalise, timeout, settle) is the unit under test, not the query transport.
const mockJobQuery: {
  data: { status: ExportJobStatus; error: string | null } | undefined
  error?: unknown
} = { data: undefined }
vi.mock('./queries', () => ({ useExportJob: () => mockJobQuery }))

// Registers one tracked job on mount and surfaces the live active count.
function Harness() {
  const { trackExport, activeCount } = useExports()
  useEffect(() => {
    trackExport({ id: 'job-1', template: 'transcript' }, 'evaluation-group-g1')
  }, [trackExport])
  return <div data-testid="count">{activeCount}</div>
}

function renderProvider() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ExportsProvider>
        <Harness />
      </ExportsProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.error).mockClear()
  mockJobQuery.data = undefined
  mockJobQuery.error = undefined
})

describe('ExportsProvider / JobWatcher', () => {
  it('toasts success with a Download action when a tracked job turns ready', async () => {
    mockJobQuery.data = { status: 'ready', error: null }

    renderProvider()

    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1))
    const [message, opts] = vi.mocked(toast.success).mock.calls[0]!
    expect(message).toContain('transcript')
    expect((opts as { action?: { label: string } }).action?.label).toBe('Download')
    // Settled → dropped from the active set.
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('0'))
  })

  it('toasts the failure reason when a tracked job fails', async () => {
    mockJobQuery.data = { status: 'failed', error: 'boom' }

    renderProvider()

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1))
    expect(vi.mocked(toast.error).mock.calls[0]![0]).toContain('boom')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('counts the job as active while it is still generating', async () => {
    mockJobQuery.data = { status: 'pending', error: null }

    renderProvider()

    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('1'))
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('does not double-track the same job id (impatient double-click)', async () => {
    mockJobQuery.data = { status: 'pending', error: null }

    function DoubleHarness() {
      const { trackExport, activeCount } = useExports()
      useEffect(() => {
        trackExport({ id: 'dup', template: 't' }, 'stem')
        trackExport({ id: 'dup', template: 't' }, 'stem')
      }, [trackExport])
      return <div data-testid="count">{activeCount}</div>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ExportsProvider>
          <DoubleHarness />
        </ExportsProvider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('1'))
  })

  describe('with fake timers', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('gives up with an error toast when a job never finalises', () => {
      mockJobQuery.data = { status: 'pending', error: null }

      renderProvider()

      expect(toast.error).not.toHaveBeenCalled()
      // Past the 90s client-side timeout.
      act(() => vi.advanceTimersByTime(91_000))

      expect(toast.error).toHaveBeenCalledTimes(1)
      expect(vi.mocked(toast.error).mock.calls[0]![0]).toMatch(/too long/i)
    })

    it('stops watching without a timeout toast when the poll is refused for consent', () => {
      // The job is not slow, it is unreachable until the gate is cleared — so the timeout's
      // "taking too long" would be a false diagnosis on top of the acceptance screen.
      mockJobQuery.error = new ApiError({ type: CONSENT_REQUIRED_TYPE }, 403)

      renderProvider()
      act(() => vi.advanceTimersByTime(91_000))

      expect(toast.error).not.toHaveBeenCalled()
      expect(screen.getByTestId('count')).toHaveTextContent('0')
    })
  })
})
