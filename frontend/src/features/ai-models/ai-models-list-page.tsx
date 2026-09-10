import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { KeyRound, Plus, Upload } from 'lucide-react'
import { useAiModels } from './queries'
import { useRestoreModel } from './mutations'
import {
  IMAGE_UNCONFIRMED_LABEL,
  modalitySummary,
  modelStatus,
  providerLabel,
  type ModelKind,
} from './labels'
import { BulkImportModelsDialog, BulkSetApiKeysDialog } from './bulk-import-dialog'
import { ModelKindDialog } from './model-kind-dialog'
import { ListViewControls, ResetViewButton } from '@/features/saved-views/list-view-controls'
import { useListViewState } from '@/features/saved-views/use-list-view-state'
import { PageHeader } from '@/components/shared/page-header'
import { DataTable, type Column } from '@/components/shared/data-table'
import { DeletedToggle } from '@/components/shared/deleted-toggle'
import { Pagination } from '@/components/shared/pagination'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { AiModelResponse } from '@/lib/api/types'

const PAGE_SIZE = 20
type ModelFilters = { deleted: boolean }
const DEFAULT_FILTERS: ModelFilters = { deleted: false }

const columns: Column<AiModelResponse>[] = [
  {
    id: 'name',
    label: 'Name',
    header: 'Name',
    cell: (m) => <span className="font-medium">{m.name}</span>,
  },
  {
    id: 'description',
    label: 'Description',
    header: 'Description',
    hideBelow: 'md',
    // One line per row whatever the note's length; the full text is on the profile.
    cell: (m) => (
      // `line-clamp-1` is visual only, so a sighted mouse user has no other way to read the rest.
      <span
        className="text-muted-foreground line-clamp-1 max-w-md"
        title={m.description ?? undefined}
      >
        {m.description?.trim() || '—'}
      </span>
    ),
  },
  {
    id: 'alias',
    label: 'Alias',
    header: 'Alias',
    hideBelow: 'lg',
    cell: (m) => <span className="font-mono text-xs">{m.model_alias}</span>,
  },
  {
    id: 'provider',
    label: 'Provider',
    header: 'Provider',
    hideBelow: 'md',
    cell: (m) => providerLabel(m.provider),
  },
  {
    id: 'modality',
    label: 'Modality',
    header: 'Modality',
    hideBelow: 'lg',
    // The doubt belongs next to what is doubted, not in Status — a mismatch does not stop the model
    // being used, it means the last check couldn't confirm the claim. Marker only, no `title`: the
    // provider text runs to 255 chars and AT read `title` on a generic node during a row sweep, so
    // putting it here would undo the short accessible name the detail page's hint exists to give it.
    // The row is one click from that page.
    cell: (m) => (
      <span className="flex flex-col items-start gap-1">
        {modalitySummary(m.input_modalities, m.output_modalities)}
        {m.capability_mismatch && <Badge variant="warn">{IMAGE_UNCONFIRMED_LABEL}</Badge>}
      </span>
    ),
  },
  {
    id: 'labels',
    label: 'Labels',
    header: 'Labels',
    hideBelow: 'lg',
    cell: (m) =>
      m.labels.length > 0 ? (
        // Capped and titled like the conversation tags: a label runs to 64 chars and 20 of them
        // share a cell in an eight-column table, so an uncapped set owns the row height.
        <span className="flex flex-wrap gap-1">
          {m.labels.map((l) => (
            <Badge key={l} variant="tag" className="max-w-[12rem]" title={l}>
              <span className="truncate">{l}</span>
            </Badge>
          ))}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    id: 'status',
    label: 'Status',
    header: 'Status',
    cell: (m) => {
      const status = modelStatus(m)
      return <Badge variant={status.variant}>{status.label}</Badge>
    },
  },
  {
    id: 'api_key',
    label: 'API key',
    header: 'API key',
    hideBelow: 'lg',
    cell: (m) => (m.has_api_key ? 'Yes' : 'No'),
  },
]

export function AiModelsListPage() {
  const navigate = useNavigate()
  const { has } = usePermissions()
  const [bulkImportOpen, setBulkImportOpen] = useState(false)
  const [bulkKeysOpen, setBulkKeysOpen] = useState(false)
  // `?new` is how the create route hands a kind-less visitor back here, so a
  // bookmarked /ai-models/new still lands on the choice instead of a dead end.
  const [searchParams, setSearchParams] = useSearchParams()
  const kindOpen = searchParams.has('new')
  const openKindDialog = () => setSearchParams({ new: '' })
  const closeKindDialog = () => setSearchParams({}, { replace: true })
  // Replace, so the `?new` entry this dialog pushed is consumed: browser Back from the
  // form lands on the list, not on the chooser the user just came through.
  const chooseKind = (kind: ModelKind) => navigate(`/ai-models/new?kind=${kind}`, { replace: true })
  const view = useListViewState<ModelFilters>({ defaultFilters: DEFAULT_FILTERS })
  const restore = useRestoreModel()
  // Gate the whole mode, not just the toggle: a saved view keeps `deleted` in its filter
  // blob, so one saved while browsing tombstones would otherwise re-enter them for a
  // caller who has since lost `models:delete` — onto Restore buttons the server refuses.
  const viewingDeleted = view.filters.deleted && has('models:delete')
  const query = useAiModels({ limit: PAGE_SIZE, offset: view.offset, deleted: viewingDeleted })
  const page = query.data
  // A tombstone has no detail page to navigate to, so the row action replaces it — and it
  // leads the row rather than trailing it, because a trailing column is the first thing a
  // narrow viewport pushes behind a horizontal scroll, and here that would be the only
  // affordance in the view. The live `status` column goes: it answers "can I dispatch to
  // this right now?", so a tombstone rendering a green "enabled" badge is the wrong answer
  // to the wrong question.
  const deletedColumns: Column<AiModelResponse>[] = [
    {
      header: '',
      cell: (m) => (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Restore model: ${m.name}`}
          disabled={restore.isPending}
          onClick={(e) => {
            e.stopPropagation()
            restore.mutate(m.id)
          }}
        >
          Restore
        </Button>
      ),
    },
    ...columns.filter((c) => c.id !== 'status'),
    {
      id: 'deleted_at',
      label: 'Deleted',
      header: 'Deleted',
      cell: (m) => (
        <span className="text-muted-foreground">
          {m.deleted_at ? new Date(m.deleted_at).toLocaleString() : '—'}
        </span>
      ),
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader title="AI Models" description="Model registry available to the platform." />
      <div className="flex flex-wrap gap-2">
        {has('models:delete') && (
          <DeletedToggle
            value={view.filters.deleted}
            onChange={(next) => view.setFilter('deleted', next)}
            label="Model rows"
          />
        )}
        <ResetViewButton view={view} />

        <div className="flex flex-wrap gap-2 sm:ml-auto">
          <ListViewControls resource="ai-models" view={view} columns={columns} />
          {has('models:update') && (
            <Button variant="outline" onClick={() => setBulkKeysOpen(true)}>
              <KeyRound className="size-4" /> Bulk set keys
            </Button>
          )}
          {has('models:create') && (
            <>
              <Button variant="outline" onClick={() => setBulkImportOpen(true)}>
                <Upload className="size-4" /> Bulk import
              </Button>
              <Button onClick={openKindDialog}>
                <Plus className="size-4" /> New model
              </Button>
            </>
          )}
        </div>
      </div>
      <BulkImportModelsDialog open={bulkImportOpen} onOpenChange={setBulkImportOpen} />
      <BulkSetApiKeysDialog open={bulkKeysOpen} onOpenChange={setBulkKeysOpen} />
      <ModelKindDialog
        open={kindOpen}
        onOpenChange={(next) => (next ? openKindDialog() : closeKindDialog())}
        onChoose={chooseKind}
      />
      <DataTable
        columns={viewingDeleted ? deletedColumns : columns}
        rows={page?.items}
        rowKey={(m) => m.id}
        onRowClick={viewingDeleted ? undefined : (m) => navigate(`/ai-models/${m.id}`)}
        isLoading={query.isPending}
        isError={query.isError}
        error={query.error}
        hiddenColumns={view.hiddenColumns}
        emptyLabel={viewingDeleted ? 'Nothing deleted recently.' : 'No models registered yet.'}
        emptyHint={
          viewingDeleted ? 'Deleted models stay restorable for a limited time.' : undefined
        }
        emptyAction={
          has('models:create') && !viewingDeleted ? (
            <Button onClick={openKindDialog}>
              <Plus className="size-4" /> New model
            </Button>
          ) : undefined
        }
      />
      {page && (
        <Pagination
          offset={view.offset}
          limit={PAGE_SIZE}
          total={page.total}
          onOffsetChange={view.setOffset}
        />
      )}
    </div>
  )
}
