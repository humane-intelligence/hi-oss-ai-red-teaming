import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { ExportsSection } from './exports-section'
import type { CsvExportInfo, ExportJobResponse } from '@/lib/api/types'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'

const TRANSCRIPT: CsvExportInfo = {
  key: 'transcript',
  name: 'Transcript',
  description: 'Every message.',
  permission: 'conversations:read',
}

function job(overrides: Partial<ExportJobResponse>): ExportJobResponse {
  return {
    id: 'job-1',
    template: 'transcript',
    format: 'csv',
    status: 'ready',
    evaluation_id: null,
    evaluation_group_id: GROUP_ID,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-01-02T00:00:00Z',
    ...overrides,
  }
}

function templatesHandler() {
  return http.get('http://localhost/api/v1/exports', () =>
    HttpResponse.json({ items: [TRANSCRIPT], total: 1, limit: 100, offset: 0 }),
  )
}

function jobsHandler(items: ExportJobResponse[]) {
  return http.get('http://localhost/api/v1/exports/jobs', () =>
    HttpResponse.json({ items, total: items.length, limit: 50, offset: 0 }),
  )
}

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ExportsSection scope={{ evaluation_group_id: GROUP_ID }} />
    </QueryClientProvider>,
  )
}

describe('ExportsSection', () => {
  it('shows the empty state when the caller has no jobs for this scope', async () => {
    server.use(templatesHandler(), jobsHandler([]))

    renderSection()

    expect(await screen.findByText(/no exports generated yet/i)).toBeInTheDocument()
  })

  it('surfaces a humanized error when the jobs list fails', async () => {
    server.use(
      templatesHandler(),
      http.get('http://localhost/api/v1/exports/jobs', () =>
        HttpResponse.json({ detail: 'nope' }, { status: 500 }),
      ),
    )

    renderSection()

    expect(await screen.findByText(/something went wrong on the server/i)).toBeInTheDocument()
  })

  it('renders a ready job with its template name and a Download action', async () => {
    server.use(templatesHandler(), jobsHandler([job({ status: 'ready' })]))

    renderSection()

    expect(await screen.findByText('Transcript')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /download/i })).toBeInTheDocument()
  })

  it('shows a generating cue and no Download for an unfinished job', async () => {
    server.use(templatesHandler(), jobsHandler([job({ status: 'running' })]))

    renderSection()

    expect(await screen.findByText(/generating/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /download/i })).not.toBeInTheDocument()
  })

  it('marks a failed job as Failed', async () => {
    server.use(
      templatesHandler(),
      jobsHandler([job({ status: 'failed', error: 'boom', expires_at: null })]),
    )

    renderSection()

    expect(await screen.findByText(/^failed$/i)).toBeInTheDocument()
  })

  it('offers a delete button for a finished job but not while it is generating', async () => {
    server.use(templatesHandler(), jobsHandler([job({ status: 'running' })]))
    renderSection()
    await screen.findByText('Transcript')
    expect(screen.queryByRole('button', { name: /delete export/i })).not.toBeInTheDocument()
  })

  it('offers a delete button for a failed job (clearing failed exports is a core use case)', async () => {
    server.use(
      templatesHandler(),
      jobsHandler([job({ status: 'failed', error: 'boom', expires_at: null })]),
    )
    renderSection()
    await screen.findByText(/^failed$/i)
    expect(screen.getByRole('button', { name: /delete export/i })).toBeInTheDocument()
  })

  it('deletes a finished export after confirming', async () => {
    let deletedId: string | null = null
    server.use(
      templatesHandler(),
      jobsHandler([job({ id: 'job-1', status: 'ready' })]),
      http.delete('http://localhost/api/v1/exports/jobs/job-1', () => {
        deletedId = 'job-1'
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderSection()

    await user.click(await screen.findByRole('button', { name: /delete export/i }))
    // Confirm in the dialog (its button is labelled just "Delete").
    await user.click(await screen.findByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(deletedId).toBe('job-1'))
  })

  it('keeps the confirm dialog open and disabled while the delete is in flight (blocks a double-submit)', async () => {
    let releaseDelete: () => void = () => {}
    const inFlight = new Promise<void>((resolve) => {
      releaseDelete = resolve
    })
    let deleteCalls = 0
    server.use(
      templatesHandler(),
      jobsHandler([job({ id: 'job-1', status: 'ready' })]),
      http.delete('http://localhost/api/v1/exports/jobs/job-1', async () => {
        deleteCalls += 1
        await inFlight // hold the response open to keep the mutation pending
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderSection()

    await user.click(await screen.findByRole('button', { name: /delete export/i }))
    await user.click(await screen.findByRole('button', { name: /^delete$/i }))

    // In flight: the dialog stays open with a disabled "Working…" confirm (it closes only on success),
    // so the open modal — not a per-row guard — blocks a second submit.
    await waitFor(() => expect(screen.getByRole('button', { name: /working/i })).toBeDisabled())

    releaseDelete()
    // On success the dialog closes (repo convention).
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /working/i })).not.toBeInTheDocument(),
    )
    expect(deleteCalls).toBe(1)
  })
})
