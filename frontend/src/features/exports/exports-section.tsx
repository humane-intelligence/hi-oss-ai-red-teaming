import { useState } from 'react'
import { Download, Loader2, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { downloadFile } from '@/lib/api/download'
import { humanizeError, isConsentRequired } from '@/lib/api/problem'
import { useDeleteExportJob } from './mutations'
import {
  exportFilename,
  exportFilenameStem,
  useExportJobs,
  useExportTemplates,
  type ExportScope,
} from './queries'

function downloadJob(jobId: string, scope: ExportScope, template: string) {
  return downloadFile(
    `/api/v1/exports/jobs/${jobId}/download`,
    exportFilename(exportFilenameStem(scope), template),
  ).catch((error: unknown) => {
    // The acceptance gate already reports a consent refusal; a toast beside it would misdiagnose
    // a refusal as a broken download.
    if (!isConsentRequired(error)) toast.error('Download failed')
  })
}

// Detail-page downloads tab: the caller's jobs for this scope, each downloadable once ready.
// Self-refreshes while any job is generating.
export function ExportsSection({ scope }: { scope: ExportScope }) {
  const query = useExportJobs(scope)
  const items = query.data?.items ?? []
  // Only fetch the template catalog once there's a job to name — the zero-jobs path returns
  // early below without ever reading it, so an unconditional fetch would be a wasted round-trip.
  const templates = useExportTemplates(items.length > 0)
  const nameOf = (key: string) => templates.data?.items.find((t) => t.key === key)?.name ?? key
  const deleteJob = useDeleteExportJob()
  // Which job the trash button has queued for confirmation (null = dialog closed).
  const [confirmId, setConfirmId] = useState<string | null>(null)

  if (query.isPending) {
    return (
      <p className="text-muted-foreground flex items-center gap-1.5 text-sm">
        <Loader2 className="size-3.5 animate-spin" /> Loading exports…
      </p>
    )
  }
  if (query.isError) {
    return <p className="text-destructive text-sm">{humanizeError(query.error)}</p>
  }
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No exports generated yet. Use Export to generate a downloadable CSV.
      </p>
    )
  }

  return (
    <>
      <div className="space-y-2">
        {items.map((job) => {
          const isReady = job.status === 'ready'
          // A finished job (ready or failed) can be deleted; while it's still generating there's
          // nothing to remove yet, so the trash button only shows on terminal states.
          const isTerminal = isReady || job.status === 'failed'
          return (
            <div key={job.id} className="rounded-md border">
              <div className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
                <span className="min-w-0">
                  <span className="block truncate font-medium">{nameOf(job.template)}</span>
                  <span className="text-muted-foreground block truncate text-xs">
                    {new Date(job.created_at).toLocaleString()}
                  </span>
                </span>
                <div className="flex shrink-0 items-center gap-2">
                  {isReady ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => downloadJob(job.id, scope, job.template)}
                    >
                      <Download className="size-4" /> Download
                    </Button>
                  ) : job.status === 'failed' ? (
                    <span className="text-destructive text-xs" title={job.error ?? undefined}>
                      Failed
                    </span>
                  ) : (
                    <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
                      <Loader2 className="size-3.5 animate-spin" /> Generating…
                    </span>
                  )}
                  {isTerminal && (
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Delete export"
                      onClick={() => setConfirmId(job.id)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
      <ConfirmDialog
        open={confirmId !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmId(null)
        }}
        title="Delete this export?"
        description="The generated CSV is removed permanently and can no longer be downloaded. This can't be undone."
        confirmLabel="Delete"
        destructive
        pending={deleteJob.isPending}
        onConfirm={() => {
          // Close on success (repo convention): the dialog stays open + `pending` disables confirm
          // while the delete is in flight, so it — not a per-row guard — blocks a double-submit.
          if (confirmId) deleteJob.mutate(confirmId, { onSuccess: () => setConfirmId(null) })
        }}
      />
    </>
  )
}
