import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'

// Exactly one scope target — a single evaluation or a whole group.
export type ExportScope = { evaluation_id: string } | { evaluation_group_id: string }

const ACTIVE_STATUSES = new Set(['pending', 'running'])

// Once an active job has been running this long, back OFF the poll interval (below) rather than
// stopping — a genuinely wedged job is failed by the server reaper (→ terminal, polling ends on
// its own), so we must never freeze a slow-but-successful export on 'Generating…', but we also
// don't need to keep hammering every 2s once it's clearly not the ~1s happy path.
const ACTIVE_POLL_BACKOFF_MS = 60_000

// Download filename stem for a scope, e.g. `evaluation-<id>` / `evaluation-group-<id>`.
export function exportFilenameStem(scope: ExportScope): string {
  return 'evaluation_id' in scope
    ? `evaluation-${scope.evaluation_id}`
    : `evaluation-group-${scope.evaluation_group_id}`
}

// Fallback download filename when the response carries no Content-Disposition — mirrors the
// backend `_job_filename` convention (`<scope-stem>-<template>.csv`), kept in one place so the
// download call sites can't drift from the server.
export function exportFilename(filenameStem: string, template: string): string {
  return `${filenameStem}-${template}.csv`
}

// The CSV export catalog — the pick-list for the export dialog. `enabled` lets the dialog defer
// the fetch until it opens, so a mounted-but-closed dialog on every detail page costs nothing.
export function useExportTemplates(enabled = true) {
  return useQuery({
    queryKey: ['exports'],
    enabled,
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/exports', { params: { query: { limit: 100, offset: 0 } } }),
      ),
  })
}

// Poll one job until it finalises; the interval stops once it reaches `ready`/`failed`.
export function useExportJob(jobId: string | null) {
  return useQuery({
    queryKey: ['export-job', jobId],
    enabled: jobId !== null,
    // A transient poll blip keeps polling by design (below); don't pop a global error toast for
    // it — the terminal ready/failed toast is the single source of truth for the export outcome.
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/exports/jobs/{job_id}', {
          params: { path: { job_id: jobId ?? '' } },
        }),
      ),
    // Keep polling until a terminal status. A failed/blank first poll (transient blip) leaves the
    // job undefined — keep polling then too, else the watcher freezes until its 90s timeout and
    // reports a false failure for an export that actually succeeded. Once active past the backoff
    // threshold, slow down like `useExportJobs` rather than hammering 1.5s for the whole wait.
    refetchInterval: (query) => {
      const job = query.state.data
      if (job === undefined) return 1500
      if (!ACTIVE_STATUSES.has(job.status)) return false
      const ageMs = Date.now() - new Date(job.created_at).getTime()
      return ageMs > ACTIVE_POLL_BACKOFF_MS ? 15_000 : 1500
    },
  })
}

// The caller's export jobs for one scope; self-refreshes while any is still generating.
export function useExportJobs(scope: ExportScope) {
  return useQuery({
    queryKey: ['export-jobs', scope],
    // Transient poll blips shouldn't toast — the row's own status conveys the outcome.
    meta: { suppressErrorToast: true },
    queryFn: async () =>
      unwrap(
        await apiClient.GET('/api/v1/exports/jobs', {
          params: { query: { ...scope, limit: 50, offset: 0 } },
        }),
      ),
    // Poll while any job is active, terminating when it reaches a terminal state (finished, or
    // failed by the server reaper). Back off to a slow interval once the oldest active job passes
    // the backoff threshold — never stop, so a legitimately long export still gets its terminal
    // poll, but don't hammer every 2s for the whole wait.
    refetchInterval: (query) => {
      const active = (query.state.data?.items ?? []).filter((job) =>
        ACTIVE_STATUSES.has(job.status),
      )
      if (active.length === 0) return false
      const oldestActiveAgeMs = Math.max(
        ...active.map((job) => Date.now() - new Date(job.created_at).getTime()),
      )
      return oldestActiveAgeMs > ACTIVE_POLL_BACKOFF_MS ? 15_000 : 2000
    },
  })
}
