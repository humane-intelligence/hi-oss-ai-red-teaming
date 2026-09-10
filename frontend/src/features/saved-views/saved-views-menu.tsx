import { useState } from 'react'
import { Bookmark, Pencil, Plus, Trash2 } from 'lucide-react'
import { Dropdown } from '@/components/ui/dropdown'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { SaveViewDialog } from './save-view-dialog'
import { useSavedViews } from './queries'
import { useDeleteSavedView } from './mutations'
import type { ListViewState } from './use-list-view-state'
import type { SavedViewResource, SavedViewResponse } from '@/lib/api/types'

// Views menu for one list: recall a saved view (applies its state), or save the
// current state as a new view. Each row offers rename + delete.
export function SavedViewsMenu<F extends Record<string, unknown>>({
  resource,
  view,
}: {
  resource: SavedViewResource
  view: ListViewState<F>
}) {
  const { data } = useSavedViews(resource)
  const del = useDeleteSavedView(resource)
  const [saveOpen, setSaveOpen] = useState(false)
  const [renameTarget, setRenameTarget] = useState<SavedViewResponse | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<SavedViewResponse | null>(null)

  const views = data?.items ?? []

  return (
    <>
      <Dropdown
        ariaLabel="Saved views"
        label={
          <>
            <Bookmark className="size-4" /> Views
          </>
        }
        panelClassName="min-w-64"
      >
        {(close) => (
          // Matches the columns menu it shares a toolbar with.
          <div className="select-none">
            <button
              type="button"
              className="hover:bg-accent flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm"
              onClick={() => {
                setSaveOpen(true)
                close()
              }}
            >
              <Plus className="size-4" /> Save current view…
            </button>
            {views.length > 0 && <div className="my-1 border-t" />}
            {views.map((v) => (
              <div
                key={v.id}
                className="hover:bg-accent flex items-center gap-1 rounded pr-1 text-sm"
              >
                <button
                  type="button"
                  // Truncated and now unselectable, so the full name needs somewhere to show.
                  title={v.name}
                  className="flex-1 truncate px-2 py-1.5 text-left"
                  onClick={() => {
                    view.apply(v.state)
                    close()
                  }}
                >
                  {v.name}
                </button>
                <button
                  type="button"
                  aria-label={`Rename ${v.name}`}
                  className="text-muted-foreground hover:text-foreground rounded p-1"
                  onClick={() => {
                    setRenameTarget(v)
                    close()
                  }}
                >
                  <Pencil className="size-3.5" />
                </button>
                <button
                  type="button"
                  aria-label={`Delete ${v.name}`}
                  className="text-muted-foreground hover:text-destructive rounded p-1"
                  onClick={() => {
                    setDeleteTarget(v)
                    close()
                  }}
                >
                  <Trash2 className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </Dropdown>

      <SaveViewDialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        resource={resource}
        getState={view.serialize}
      />
      <SaveViewDialog
        open={renameTarget !== null}
        onOpenChange={(open) => !open && setRenameTarget(null)}
        resource={resource}
        view={renameTarget}
        getState={view.serialize}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="Delete saved view"
        description={
          // Views have no browsable tombstone surface, so the only way back is the Undo on
          // the toast that follows — the shared "restorable for a limited time" note would
          // promise a window that outlives the toast it depends on.
          deleteTarget
            ? `Delete “${deleteTarget.name}”? You can undo this from the confirmation that follows.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          deleteTarget && del.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) })
        }
      />
    </>
  )
}
