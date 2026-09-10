import { Controller, useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ApiError } from '@/lib/api/problem'
import { useBulkInviteToGroup } from './mutations'
import { MAX_INVITE_ROWS } from '@/lib/api/limits'
import { RolesPicker } from '@/features/users/roles-picker'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import type {
  BulkGroupInviteResponse,
  BulkGroupInviteRowResult,
  GroupInvitationBulkRequest,
} from '@/lib/api/types'

// Dedup lowercase like the server does — case variants would otherwise 422 the whole envelope.
function parseEmails(raw: string): string[] {
  const seen = new Set<string>()
  const emails: string[] = []
  for (const part of raw.split(/[\s,;]+/)) {
    const email = part.toLowerCase()
    if (email && !seen.has(email)) {
      seen.add(email)
      emails.push(email)
    }
  }
  return emails
}

const ROLES_ERROR_ID = 'bulk-invite-roles-error'

const isEmail = (v: string) => z.string().email().safeParse(v).success

const schema = z.object({
  raw: z
    .string()
    .refine((v) => {
      const emails = parseEmails(v)
      return emails.length > 0 && emails.every(isEmail)
    }, 'Enter valid emails, one per line or comma-separated')
    .refine(
      (v) => parseEmails(v).length <= MAX_INVITE_ROWS,
      `At most ${MAX_INVITE_ROWS} emails per request`,
    ),
  role_ids: z.array(z.string()).min(1, 'Pick at least one role'),
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = { raw: '', role_ids: [] }

function FailedRow({ row, email }: { row: BulkGroupInviteRowResult; email: string }) {
  const reason = row.error?.detail ?? row.error?.title ?? 'Unknown error'
  return (
    <li className="text-sm">
      <span className="font-medium">{email}</span>: {reason}
    </li>
  )
}

function Summary({
  summary,
  rows,
}: {
  summary: BulkGroupInviteResponse
  rows: GroupInvitationBulkRequest['rows']
}) {
  // `data` is null on failed rows, so the sent email comes from the request.
  const emailByKey = new Map(rows.map((r) => [r.row_key, r.data.email]))
  const failed = summary.results.filter((r) => r.status === 'failed')
  return (
    <div className="space-y-2">
      <p className="text-sm">
        {summary.succeeded} of {summary.total} succeeded
        {summary.failed > 0 && `, ${summary.failed} failed`}.
      </p>
      {failed.length > 0 && (
        <ul className="list-disc space-y-1 pl-4">
          {failed.map((r) => (
            <FailedRow
              key={r.row_key}
              row={r}
              email={emailByKey.get(r.row_key) ?? `row ${Number(r.row_key) + 1}`}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

export function BulkInviteDialog({
  groupId,
  open,
  onOpenChange,
}: {
  groupId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const bulk = useBulkInviteToGroup(groupId)
  const { register, control, handleSubmit, reset, setError, formState } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  const raw = useWatch({ control, name: 'raw' })
  const parsedCount = parseEmails(raw).length

  const onSubmit = handleSubmit(async (values) => {
    const rows = parseEmails(values.raw).map((email, i) => ({
      row_key: String(i),
      data: { email, role_ids: values.role_ids },
    }))
    try {
      await bulk.mutateAsync({ rows, dry_run: false })
    } catch (err) {
      // A 422's errors[] names no field of this form and its global toast is suppressed — show it inline.
      if (err instanceof ApiError && err.problem.errors?.length) {
        setError('raw', { message: err.problem.errors.map((e) => e.msg).join('; ') })
      }
    }
  })

  function handleOpenChange(next: boolean) {
    if (!next) bulk.reset()
    onOpenChange(next)
  }

  return (
    <Modal
      open={open}
      onOpenChange={handleOpenChange}
      title="Invite by email"
      onOpen={() => {
        bulk.reset()
        reset(EMPTY)
      }}
    >
      {bulk.isSuccess ? (
        <>
          <Summary summary={bulk.data} rows={bulk.variables?.rows ?? []} />
          <div className="flex justify-end">
            <Button onClick={() => handleOpenChange(false)}>Close</Button>
          </div>
        </>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <FormField
            label="Emails (one per line or comma-separated)"
            htmlFor="bulk-emails"
            hint={`Up to ${MAX_INVITE_ROWS} invitees per request.`}
            error={formState.errors.raw?.message}
          >
            <Textarea
              id="bulk-emails"
              rows={6}
              placeholder="alice@example.com&#10;bob@example.com"
              {...register('raw')}
            />
            {parsedCount > 0 && (
              // FormField's hint is a hover-only tooltip; the counter sits where the eye already is.
              <p
                className={
                  parsedCount > MAX_INVITE_ROWS
                    ? 'text-destructive text-xs'
                    : 'text-muted-foreground text-xs'
                }
              >
                {parsedCount} of {MAX_INVITE_ROWS} email(s) parsed
              </p>
            )}
          </FormField>
          <FormField
            label="Roles"
            error={formState.errors.role_ids?.message}
            errorId={ROLES_ERROR_ID}
          >
            <Controller
              control={control}
              name="role_ids"
              render={({ field }) => (
                <RolesPicker
                  selected={field.value}
                  onChange={field.onChange}
                  objectAssignableOnly
                  // Wired by hand: `Controller` renders through a prop, so cloned aria props from
                  // `FormField` would land on it and go nowhere.
                  describedBy={formState.errors.role_ids ? ROLES_ERROR_ID : undefined}
                  invalid={formState.errors.role_ids ? true : undefined}
                />
              )}
            />
          </FormField>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => handleOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={formState.isSubmitting}>
              {formState.isSubmitting
                ? 'Sending…'
                : parsedCount > 0
                  ? `Send ${parsedCount} invite${parsedCount === 1 ? '' : 's'}`
                  : 'Send invites'}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  )
}
