import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { downloadFile } from '@/lib/api/download'
import { isConsentRequired } from '@/lib/api/problem'
import { ExportsContext, type ExportsContextValue } from './exports-context'
import { exportFilename, useExportJob } from './queries'

// Give up polling a job after this long so a stuck worker surfaces as an error
// instead of an endless "Exporting…". Normal jobs finish in ~1s.
const EXPORT_TIMEOUT_MS = 90_000

type TrackedJob = { id: string; template: string; filenameStem: string }

// App-wide background export tracker: a queued job runs here (nav progress cue + a
// ready/failed toast) so Export never blocks the UI.
export function ExportsProvider({ children }: { children: ReactNode }) {
  const [tracked, setTracked] = useState<TrackedJob[]>([])

  const trackExport = useCallback((job: { id: string; template: string }, filenameStem: string) => {
    setTracked((prev) =>
      prev.some((j) => j.id === job.id) ? prev : [...prev, { ...job, filenameStem }],
    )
  }, [])
  const settle = useCallback(
    (id: string) => setTracked((prev) => prev.filter((j) => j.id !== id)),
    [],
  )

  const value = useMemo<ExportsContextValue>(
    () => ({ trackExport, activeCount: tracked.length }),
    [trackExport, tracked.length],
  )

  return (
    <ExportsContext.Provider value={value}>
      {children}
      {tracked.map((job) => (
        <JobWatcher key={job.id} job={job} onSettled={settle} />
      ))}
    </ExportsContext.Provider>
  )
}

// Headless: polls one tracked job, fires the terminal toast once, then unmounts via onSettled.
function JobWatcher({ job, onSettled }: { job: TrackedJob; onSettled: (id: string) => void }) {
  const query = useExportJob(job.id)
  const qc = useQueryClient()
  const status = query.data?.status
  const error = query.data?.error
  const refused = isConsentRequired(query.error)
  const doneRef = useRef(false)

  // A version published mid-export refuses the poll: the job is not slow, it is unreachable until
  // the gate is cleared. Stop watching rather than letting the timeout below diagnose a timeout.
  useEffect(() => {
    if (doneRef.current || !refused) return
    doneRef.current = true
    onSettled(job.id)
  }, [refused, job.id, onSettled])

  useEffect(() => {
    const timer = setTimeout(() => {
      if (doneRef.current) return
      doneRef.current = true
      toast.error('Export is taking too long — check the exports list on the detail page later.')
      onSettled(job.id)
    }, EXPORT_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [job.id, onSettled])

  useEffect(() => {
    if (doneRef.current || (status !== 'ready' && status !== 'failed')) return
    doneRef.current = true
    qc.invalidateQueries({ queryKey: ['export-jobs'] })
    if (status === 'ready') {
      toast.success(`Export ready: ${job.template}`, {
        duration: 10_000,
        action: {
          label: 'Download',
          onClick: () =>
            downloadFile(
              `/api/v1/exports/jobs/${job.id}/download`,
              exportFilename(job.filenameStem, job.template),
            ).catch((err: unknown) => {
              if (!isConsentRequired(err)) toast.error('Download failed')
            }),
        },
      })
    } else {
      toast.error(error ? `Export failed: ${error}` : 'Export failed')
    }
    onSettled(job.id)
  }, [status, error, job, qc, onSettled])

  return null
}
