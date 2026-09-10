import { useRef, useState, type ChangeEvent } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ClipboardList, Plus, Upload, X } from 'lucide-react'
import { MAX_INVITE_ROWS } from '@/lib/api/limits'
import { ApiError } from '@/lib/api/problem'
import { useBulkInvite } from './mutations'
import { useRoles } from './queries'
import { BulkResultSummary } from './bulk-result-dialog'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import type { BulkInviteRequest, RoleResponse } from '@/lib/api/types'

const rowSchema = z.object({
  email: z.string().min(1, 'Required').email('Invalid email'),
  role_ids: z.array(z.string()).min(1, 'Pick at least one role'),
})

const schema = z.object({
  rows: z
    .array(rowSchema)
    .min(1, 'Add at least one invitee')
    .max(MAX_INVITE_ROWS, `At most ${MAX_INVITE_ROWS} invitees per request`)
    // Flag case-variant duplicates on their row — the server would 422 the whole envelope.
    .superRefine((rows, ctx) => {
      const seen = new Map<string, number>()
      rows.forEach((row, i) => {
        const key = row.email.trim().toLowerCase()
        if (!key) return
        const first = seen.get(key)
        if (first === undefined) seen.set(key, i)
        else
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: [i, 'email'],
            message: `Duplicate of row ${first + 1}`,
          })
      })
    }),
})
type FormValues = z.infer<typeof schema>

const emptyRow = (): FormValues['rows'][number] => ({ email: '', role_ids: [] })

function parseEmailList(raw: string): string[] {
  const seen = new Set<string>()
  const emails: string[] = []
  for (const part of raw.split(/[\s,;]+/)) {
    const email = part.trim().toLowerCase()
    if (email && !seen.has(email)) {
      seen.add(email)
      emails.push(email)
    }
  }
  return emails
}

const isEmail = (v: string) => z.string().email().safeParse(v).success

type CsvParse = { ok: true; rows: FormValues['rows'] } | { ok: false; errors: string[] }

// Two columns (email, role), one role per line — the row is editable after import.
// All-or-nothing: any bad line rejects the file, so a typo can't silently drop invitees.
function parseCsv(text: string, roles: RoleResponse[], existing: FormValues['rows']): CsvParse {
  const roleByKey = new Map<string, string>()
  for (const r of roles) {
    roleByKey.set(r.name.toLowerCase(), r.id)
    roleByKey.set(r.display_name.toLowerCase(), r.id)
  }
  const seen = new Set(existing.map((r) => r.email.trim().toLowerCase()).filter(Boolean))
  const rows: FormValues['rows'] = []
  const errors: string[] = []
  text.split(/\r?\n/).forEach((line, idx) => {
    if (!line.trim()) return
    const cells = line.split(',').map((c) => c.trim().replace(/^"|"$/g, ''))
    const [rawEmail = '', rawRole = ''] = cells
    if (idx === 0 && rawEmail.toLowerCase() === 'email') return
    const n = idx + 1
    if (cells.length !== 2) {
      errors.push(`Line ${n}: expected two columns (email, role), got ${cells.length}`)
      return
    }
    const email = rawEmail.toLowerCase()
    if (!isEmail(email)) {
      errors.push(`Line ${n}: invalid email "${rawEmail}"`)
      return
    }
    const roleId = roleByKey.get(rawRole.toLowerCase())
    if (!roleId) {
      errors.push(`Line ${n}: unknown role "${rawRole}"`)
      return
    }
    if (seen.has(email)) {
      errors.push(`Line ${n}: duplicate email "${email}"`)
      return
    }
    seen.add(email)
    rows.push({ email, role_ids: [roleId] })
  })
  if (rows.length === 0 && errors.length === 0) errors.push('No data rows found.')
  const total = existing.filter((r) => r.email.trim() !== '').length + rows.length
  if (total > MAX_INVITE_ROWS) {
    errors.push(`The file brings the total to ${total} rows; the cap is ${MAX_INVITE_ROWS}.`)
  }
  return errors.length > 0 ? { ok: false, errors } : { ok: true, rows }
}

export function RoleChips({
  value,
  onChange,
  label,
}: {
  value?: string[]
  onChange: (next: string[]) => void
  label: string
}) {
  const { data, isPending } = useRoles()
  const roles = data?.items ?? []
  // A field-array replace() can hand the Controller one undefined-valued render
  // before RHF settles — don't crash on it.
  const selected = value ?? []
  if (isPending) return <p className="text-muted-foreground text-xs">Loading roles…</p>
  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1">
      {roles.map((r) => (
        <button
          key={r.id}
          type="button"
          aria-pressed={selected.includes(r.id)}
          onClick={() => toggle(r.id)}
          className={cn(
            'rounded-md border px-2 py-0.5 text-xs',
            selected.includes(r.id)
              ? 'bg-primary text-primary-foreground border-transparent'
              : 'text-muted-foreground hover:bg-muted',
          )}
        >
          {r.display_name}
        </button>
      ))}
    </div>
  )
}

function inviteeLabel(rows: BulkInviteRequest['rows']) {
  const emailByKey = new Map(rows.map((r) => [r.row_key, r.data.email]))
  return (key: string) => emailByKey.get(key) ?? `row ${Number(key) + 1}`
}

export function InviteDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Invite users" className="max-w-2xl">
      {/* Remount per open: resetting a surviving form left unmounted rows' values behind, and
          validating those blocked the next submit. */}
      <InviteForm onClose={() => onOpenChange(false)} />
    </Modal>
  )
}

function InviteForm({ onClose }: { onClose: () => void }) {
  const bulk = useBulkInvite()
  const {
    control,
    register,
    handleSubmit,
    getValues,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { rows: [emptyRow()] },
  })
  const { fields, append, remove, replace } = useFieldArray({ control, name: 'rows' })
  const [pasteOpen, setPasteOpen] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const [csvErrors, setCsvErrors] = useState<string[]>([])
  const csvInputRef = useRef<HTMLInputElement>(null)
  const { data: rolesData } = useRoles()

  const lastRoles = () => getValues('rows').at(-1)?.role_ids ?? []

  function addPastedRows() {
    const current = getValues('rows')
    const existing = new Set(current.map((r) => r.email.trim().toLowerCase()).filter(Boolean))
    const roles = lastRoles()
    // A pasted duplicate carries no information — drop it silently. (CSV errors instead:
    // its duplicate may carry a conflicting role, and dropping that would discard data.)
    const fresh = parseEmailList(pasteText).filter((email) => !existing.has(email))
    const kept = current.filter((r) => r.email.trim() !== '')
    const next = [...kept, ...fresh.map((email) => ({ email, role_ids: roles }))]
    replace(next.length > 0 ? next : [emptyRow()])
    setPasteOpen(false)
    setPasteText('')
  }

  async function handleCsvFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // let the same file be re-picked after an edit
    if (!file) return
    const current = getValues('rows')
    const result = parseCsv(await file.text(), rolesData?.items ?? [], current)
    if (!result.ok) {
      setCsvErrors(result.errors)
      return
    }
    setCsvErrors([])
    const kept = current.filter((r) => r.email.trim() !== '')
    replace([...kept, ...result.rows])
  }

  const onSubmit = handleSubmit(async (values) => {
    const rows = values.rows.map((row, i) => ({
      row_key: String(i),
      data: { email: row.email.trim().toLowerCase(), role_ids: row.role_ids },
    }))
    try {
      await bulk.mutateAsync({ rows, dry_run: false })
    } catch (err) {
      // A 422's errors[] names no field of this form and its global toast is suppressed — show it inline.
      if (err instanceof ApiError && err.problem.errors?.length) {
        setError('root', { message: err.problem.errors.map((e) => e.msg).join('; ') })
      }
    }
  })

  const overCap = fields.length > MAX_INVITE_ROWS

  return (
    <>
      {bulk.isSuccess ? (
        <>
          <BulkResultSummary
            result={bulk.data}
            labelFor={inviteeLabel(bulk.variables?.rows ?? [])}
          />
          <div className="flex justify-end">
            <Button onClick={onClose}>Close</Button>
          </div>
        </>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
            {fields.map((field, i) => (
              <div key={field.id} className="space-y-2 rounded-md border p-3">
                <div className="flex items-start gap-2">
                  <div className="flex-1 space-y-1">
                    <Input
                      aria-label={`Invitee ${i + 1} email`}
                      placeholder="person@example.com"
                      aria-invalid={!!errors.rows?.[i]?.email}
                      {...register(`rows.${i}.email`)}
                    />
                    {errors.rows?.[i]?.email && (
                      <p className="text-destructive text-xs">{errors.rows[i]?.email?.message}</p>
                    )}
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove row ${i + 1}`}
                    disabled={fields.length === 1}
                    onClick={() => remove(i)}
                  >
                    <X className="size-4" />
                  </Button>
                </div>
                <Controller
                  control={control}
                  name={`rows.${i}.role_ids`}
                  render={({ field: f }) => (
                    <RoleChips
                      value={f.value}
                      onChange={f.onChange}
                      label={`Invitee ${i + 1} roles`}
                    />
                  )}
                />
                {errors.rows?.[i]?.role_ids && (
                  <p className="text-destructive text-xs">{errors.rows[i]?.role_ids?.message}</p>
                )}
              </div>
            ))}
          </div>
          {pasteOpen ? (
            <div className="space-y-2 rounded-md border p-3">
              <FormField
                label="Paste emails (comma, space or newline separated)"
                htmlFor="paste-emails"
              >
                <Textarea
                  id="paste-emails"
                  rows={3}
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                />
              </FormField>
              <p className="text-muted-foreground text-xs">
                New rows copy the roles of the last row.
              </p>
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setPasteOpen(false)
                    setPasteText('')
                  }}
                >
                  Cancel
                </Button>
                <Button type="button" size="sm" onClick={addPastedRows}>
                  Add rows
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => append({ email: '', role_ids: lastRoles() })}
              >
                <Plus className="size-4" /> Add row
              </Button>
              <Button type="button" size="sm" variant="outline" onClick={() => setPasteOpen(true)}>
                <ClipboardList className="size-4" /> Paste list
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => csvInputRef.current?.click()}
              >
                <Upload className="size-4" /> Upload CSV (email, role)
              </Button>
              <input
                ref={csvInputRef}
                type="file"
                accept=".csv,text/csv"
                className="hidden"
                aria-hidden="true"
                tabIndex={-1}
                onChange={handleCsvFile}
              />
              <span
                className={cn(
                  'ml-auto text-xs',
                  overCap ? 'text-destructive' : 'text-muted-foreground',
                )}
              >
                {fields.length} of {MAX_INVITE_ROWS} row(s)
              </span>
            </div>
          )}
          {csvErrors.length > 0 && (
            <div className="border-destructive/50 text-destructive space-y-1 rounded-md border p-3 text-xs">
              <p className="text-sm">File not imported — fix these lines and retry:</p>
              <ul className="max-h-32 list-disc space-y-0.5 overflow-y-auto pl-4">
                {csvErrors.map((msg) => (
                  <li key={msg}>{msg}</li>
                ))}
              </ul>
            </div>
          )}
          {(errors.rows?.root?.message ?? errors.rows?.message) && (
            <p className="text-destructive text-sm">
              {errors.rows?.root?.message ?? errors.rows?.message}
            </p>
          )}
          {errors.root?.message && (
            <p className="text-destructive text-sm">{errors.root.message}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting
                ? 'Sending…'
                : `Send ${fields.length} invite${fields.length === 1 ? '' : 's'}`}
            </Button>
          </div>
        </form>
      )}
    </>
  )
}
