import { useMemo, useRef, useState, type ChangeEvent } from 'react'
import { Check, Upload, X } from 'lucide-react'
import { z } from 'zod'
import { useBulkCreateModels, useBulkSetApiKeys } from './mutations'
import { MODALITIES, PROVIDERS, providerLabel } from './labels'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, humanizeError } from '@/lib/api/problem'
import type { AiModelApiKeyBulkItem, AiModelCreate, BulkModelResponse } from '@/lib/api/types'

// A row's required fields, validated client-side so malformed input is caught
// before a round-trip. The backend stays the validator of record: on success
// we forward the *original* object untouched (unknown extras like `parameters`
// pass straight through), and per-row conflicts (409/404) come back in the
// dry-run/commit response, not here.
// Retired with the modality sets. The backend rejects them by name; catching them here
// too keeps the failure per-row and readable, because that rejection arrives as one
// envelope-level 422 for the whole batch.
const RETIRED_KEYS = ['modality', 'supports_image_input'] as const

const modelRowSchema = z
  .object({
    name: z.string().min(1),
    model_alias: z.string().min(1),
    provider: z.enum(PROVIDERS),
    provider_model_id: z.string().min(1),
    // Mirrors the backend's "input must include text". A row the preview waves through
    // takes the whole batch down with one envelope-level 422 — not a per-row result — and
    // `run()` surfaces only `problem.detail` ("Request body failed validation."), naming
    // neither the row nor the field.
    input_modalities: z
      .array(z.enum(MODALITIES))
      .min(1)
      .refine((modalities) => modalities.includes('text'), { message: "must include 'text'" })
      .optional(),
    output_modalities: z.array(z.enum(MODALITIES)).min(1).optional(),
    api_key: z.string().min(1).optional(),
  })
  // `passthrough`, because a stripping parse would never see the retired keys — and this
  // dialog forwards the original object anyway, so nothing downstream depends on the strip.
  .passthrough()
  .superRefine((row, ctx) => {
    for (const key of RETIRED_KEYS) {
      if (key in row) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [key],
          message: 'retired — declare capabilities with input_modalities and output_modalities',
        })
      }
    }
  })

const apiKeyRowSchema = z.object({
  name: z.string().min(1).max(255),
  api_key: z.string().min(1),
})

const MODELS_EXAMPLE = `[
  {
    "name": "Claude Sonnet 4.6",
    "model_alias": "claude-sonnet-4-6",
    "provider": "anthropic",
    "provider_model_id": "claude-sonnet-4-6",
    "api_key": "sk-ant-…"
  }
]`

const KEYS_EXAMPLE = `[
  { "name": "Claude Sonnet 4.6", "api_key": "sk-ant-…" }
]`

type ParseResult<T> = { ok: true; rows: T[] } | { ok: false; message: string; rowErrors: string[] }

function parseRows<T>(raw: string, rowSchema: z.ZodType<T>): ParseResult<T> {
  let json: unknown
  try {
    json = JSON.parse(raw)
  } catch (e) {
    return { ok: false, message: `Invalid JSON: ${(e as Error).message}`, rowErrors: [] }
  }
  if (!Array.isArray(json))
    return { ok: false, message: 'Expected a JSON array of objects.', rowErrors: [] }
  if (json.length === 0) return { ok: false, message: 'Add at least one row.', rowErrors: [] }

  const rows: T[] = []
  const rowErrors: string[] = []
  json.forEach((el, i) => {
    const parsed = rowSchema.safeParse(el)
    if (parsed.success) {
      rows.push(el as T) // forward the original so unknown fields (e.g. `parameters`) survive
    } else {
      const issue = parsed.error.issues[0]
      const path = issue?.path.join('.') || '(row)'
      rowErrors.push(`Row ${i + 1}: ${path} — ${issue?.message ?? 'invalid'}`)
    }
  })
  if (rowErrors.length > 0)
    return { ok: false, message: `${rowErrors.length} row(s) have problems.`, rowErrors }
  return { ok: true, rows }
}

// A 422 (or any Problem carrying field errors) is suppressed from the global
// error toast, so we surface it inline instead; other failures (500, network)
// stay with the global toast.
function isFieldError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 422 || (err.problem.errors?.length ?? 0) > 0)
}

function rowLabel(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    const o = data as Record<string, unknown>
    if (typeof o.name === 'string') {
      return typeof o.provider === 'string' ? `${o.name} (${providerLabel(o.provider)})` : o.name
    }
  }
  return fallback
}

function ResultSummary({ res, rows }: { res: BulkModelResponse; rows: unknown[] }) {
  const failed = res.results.filter((r) => r.status === 'failed')
  return (
    <div className="space-y-2">
      <p className="text-sm">
        {res.succeeded} of {res.total} {res.dry_run ? 'rows would succeed' : 'succeeded'}
        {res.failed > 0 && `, ${res.failed} failed`}.
      </p>
      {failed.length > 0 && (
        <ul className="max-h-56 space-y-1 overflow-y-auto">
          {failed.map((r) => {
            const i = Number(r.row_key)
            return (
              <li key={r.row_key} className="flex gap-2 text-sm">
                <X className="text-destructive mt-0.5 size-4 shrink-0" />
                <span>
                  <span className="font-medium">{rowLabel(rows[i], `Row ${i + 1}`)}</span>:{' '}
                  {r.error?.detail ?? r.error?.title ?? 'Unknown error'}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

type BulkImportDialogProps<T> = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  intro: string
  placeholder: string
  rowSchema: z.ZodType<T>
  submit: (rows: { row_key: string; data: T }[], dryRun: boolean) => Promise<BulkModelResponse>
}

function BulkImportDialog<T>({
  open,
  onOpenChange,
  title,
  intro,
  placeholder,
  rowSchema,
  submit,
}: BulkImportDialogProps<T>) {
  const [raw, setRaw] = useState('')
  const [preview, setPreview] = useState<BulkModelResponse | null>(null)
  const [done, setDone] = useState<BulkModelResponse | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState<null | 'preview' | 'import'>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const parsed = useMemo(() => parseRows(raw, rowSchema), [raw, rowSchema])
  const count = parsed.ok ? parsed.rows.length : 0

  // A dry-run preview only describes the current text, so editing invalidates it.
  function changeRaw(value: string) {
    setRaw(value)
    setPreview(null)
    setServerError(null)
  }

  function resetAll() {
    setRaw('')
    setPreview(null)
    setDone(null)
    setServerError(null)
    setBusy(null)
  }

  async function handleFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) changeRaw(await file.text())
    e.target.value = '' // let the same file be re-picked after an edit
  }

  async function run(dryRun: boolean) {
    if (!parsed.ok) return
    const rows = parsed.rows.map((data, i) => ({ row_key: String(i), data }))
    setBusy(dryRun ? 'preview' : 'import')
    setServerError(null)
    try {
      const res = await submit(rows, dryRun)
      if (dryRun) setPreview(res)
      else setDone(res)
    } catch (err) {
      if (isFieldError(err)) setServerError(humanizeError(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      className="max-w-2xl"
      onOpen={resetAll}
    >
      {done ? (
        <>
          <ResultSummary res={done} rows={parsed.ok ? parsed.rows : []} />
          <div className="flex justify-end">
            <Button onClick={() => onOpenChange(false)}>Close</Button>
          </div>
        </>
      ) : (
        <div className="space-y-4">
          <p className="text-muted-foreground text-sm">{intro}</p>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label htmlFor="bulk-json" className="text-sm font-medium">
                Rows (JSON array)
              </label>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="text-muted-foreground hover:text-foreground text-xs"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="size-3.5" /> Upload .json
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".json,application/json"
                className="hidden"
                aria-hidden="true"
                tabIndex={-1}
                onChange={handleFile}
              />
            </div>
            <Textarea
              id="bulk-json"
              rows={10}
              className="font-mono text-xs"
              placeholder={placeholder}
              value={raw}
              onChange={(e) => changeRaw(e.target.value)}
            />
            {raw.trim() !== '' &&
              (parsed.ok ? (
                <p className="text-muted-foreground text-xs">{count} row(s) parsed</p>
              ) : (
                <div className="text-destructive space-y-1 text-xs">
                  <p>{parsed.message}</p>
                  {parsed.rowErrors.length > 0 && (
                    <ul className="max-h-32 list-disc space-y-0.5 overflow-y-auto pl-4">
                      {parsed.rowErrors.map((msg) => (
                        <li key={msg}>{msg}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
          </div>

          {serverError && <p className="text-destructive text-sm">{serverError}</p>}

          {preview && (
            <div className="bg-muted/30 rounded-lg border p-3">
              <p className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs font-medium">
                <Check className="size-3.5" /> Preview (nothing saved yet)
              </p>
              <ResultSummary res={preview} rows={parsed.ok ? parsed.rows : []} />
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={!parsed.ok || busy !== null}
              onClick={() => run(true)}
            >
              {busy === 'preview' ? 'Previewing…' : 'Preview'}
            </Button>
            <Button type="button" disabled={!parsed.ok || busy !== null} onClick={() => run(false)}>
              {busy === 'import' ? 'Importing…' : `Import ${count > 0 ? count : ''}`.trim()}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  )
}

type EntryProps = { open: boolean; onOpenChange: (open: boolean) => void }

export function BulkImportModelsDialog({ open, onOpenChange }: EntryProps) {
  const mutation = useBulkCreateModels()
  return (
    <BulkImportDialog<AiModelCreate>
      open={open}
      onOpenChange={onOpenChange}
      title="Bulk import models"
      intro="Paste or upload a JSON array of models to register. An optional per-row api_key is stored encrypted and never echoed back."
      placeholder={MODELS_EXAMPLE}
      rowSchema={modelRowSchema as unknown as z.ZodType<AiModelCreate>}
      submit={(rows, dryRun) => mutation.mutateAsync({ rows, dry_run: dryRun })}
    />
  )
}

export function BulkSetApiKeysDialog({ open, onOpenChange }: EntryProps) {
  const mutation = useBulkSetApiKeys()
  return (
    <BulkImportDialog<AiModelApiKeyBulkItem>
      open={open}
      onOpenChange={onOpenChange}
      title="Bulk set API keys"
      intro="Paste or upload a JSON array of { name, api_key }. Each row targets an existing model by its name."
      placeholder={KEYS_EXAMPLE}
      rowSchema={apiKeyRowSchema}
      submit={(rows, dryRun) => mutation.mutateAsync({ rows, dry_run: dryRun })}
    />
  )
}
