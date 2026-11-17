import { useRef, useState } from 'react'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { InfoHint } from '@/components/shared/info-hint'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { MAX_LABEL_CATEGORIES } from '@/lib/api/limits'
import type { EvaluationLabelCategoryResponse } from '@/lib/api/types'
import { LabelCategoryDialog } from './label-category-dialog'
import { useDeleteLabelCategory } from './mutations'
import { useEvaluationLabelCategories } from './queries'
import { LABELS_HINT } from './tag-hint'

export function LabelsCard({
  evaluationId,
  canManage,
}: {
  evaluationId: string
  canManage: boolean
}) {
  const categories = useEvaluationLabelCategories(evaluationId)
  const remove = useDeleteLabelCategory(evaluationId)
  // `undefined` = dialog closed, `null` = adding, a row = editing it.
  const [editing, setEditing] = useState<EvaluationLabelCategoryResponse | null | undefined>()
  const [deleting, setDeleting] = useState<EvaluationLabelCategoryResponse | null>(null)
  // A deleted row takes its focused Delete button with it; focus lands here instead of on the page.
  const addRef = useRef<HTMLButtonElement>(null)
  const justDeleted = useRef(false)

  const rows = categories.data ?? []
  const atCap = rows.length >= MAX_LABEL_CATEGORIES
  // As in AllowedTagsCard: the count survives the refetch a Retry starts, so the button stays mounted.
  const readFailed = categories.errorUpdateCount > 0 && !categories.data
  const retry = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={categories.isFetching}
      onClick={() => void categories.refetch()}
    >
      {categories.isFetching ? 'Retrying…' : 'Retry'}
    </Button>
  )

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            <CardTitle as="h3">Labels{categories.isSuccess ? ` (${rows.length})` : ''}</CardTitle>
            <InfoHint text={LABELS_HINT} />
          </div>
          {canManage && categories.isSuccess && (
            <Button
              ref={addRef}
              size="sm"
              variant="outline"
              aria-label="Add label category"
              disabled={atCap}
              onClick={() => setEditing(null)}
            >
              <Plus className="size-4" /> Add
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <div role="status">
          {readFailed ? (
            <div className="flex items-center gap-2">
              <p className="text-destructive text-sm">
                {categories.isFetching ? 'Retrying the labels…' : "Couldn't load the labels."}
              </p>
              {retry}
            </div>
          ) : categories.isError ? (
            // Nothing else retries (no refetch on focus), and the list may be stale, e.g. still showing a row just deleted.
            <div className="flex items-center gap-2">
              <p className="text-destructive text-sm">
                Couldn't refresh the labels — showing the last ones loaded.
              </p>
              {retry}
            </div>
          ) : categories.isPending ? (
            <p className="text-muted-foreground text-sm">Loading labels…</p>
          ) : rows.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              {canManage
                ? 'No labels yet. Add a category such as “language” to describe conversations in exports and reports. Labels are never sent to the model.'
                : 'No labels defined for this evaluation.'}
            </p>
          ) : null}
        </div>
        {rows.map((row) => (
          <div
            key={row.id}
            className="flex items-start justify-between gap-2 rounded-md border px-3 py-2 text-sm"
          >
            <div className="min-w-0 space-y-1">
              <div className="truncate font-medium" title={row.name}>
                {row.name}
              </div>
              {row.values.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {row.values.map((value) => (
                    <Badge key={value} variant="tag" className="max-w-[18rem]" title={value}>
                      <span className="truncate">{value}</span>
                    </Badge>
                  ))}
                </div>
              ) : (
                <div className="text-muted-foreground text-xs">Free text</div>
              )}
            </div>
            {canManage && (
              <div className="flex shrink-0 items-center">
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Edit ${row.name}`}
                  onClick={() => setEditing(row)}
                >
                  <Pencil className="size-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Delete ${row.name}`}
                  disabled={remove.isPending}
                  onClick={() => setDeleting(row)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            )}
          </div>
        ))}
        {canManage && atCap && (
          <p className="text-muted-foreground text-xs">
            Up to {MAX_LABEL_CATEGORIES} label categories per evaluation.
          </p>
        )}
      </CardContent>

      <LabelCategoryDialog
        evaluationId={evaluationId}
        open={editing !== undefined}
        onOpenChange={(open) => {
          if (!open) setEditing(undefined)
        }}
        category={editing}
      />

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (open) return
          setDeleting(null)
          // On the dialog's close event, after its own focus restore to the Delete button, or it would win.
          if (justDeleted.current) addRef.current?.focus()
          justDeleted.current = false
        }}
        title="Delete label category"
        description={`Delete "${deleting?.name}"? Conversations lose their ${deleting?.name} label, and it can't be restored.`}
        confirmLabel="Delete"
        destructive
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return
          remove.mutate(deleting.id, {
            onSuccess: () => {
              justDeleted.current = true
              setDeleting(null)
            },
          })
        }}
      />
    </Card>
  )
}
