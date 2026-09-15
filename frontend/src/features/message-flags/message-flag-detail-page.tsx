import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ExternalLink, Pencil, Trash2 } from 'lucide-react'
import { useMessageFlag, useFlagReviews } from './queries'
import { useDeleteFlag, useUpdateFlag } from './mutations'
import { verdictSummary } from '@/features/reviews/format'
import { useUserLookup } from '@/features/users/queries'
import { humanizeError, isForbidden } from '@/lib/api/problem'
import { applyApiError } from '@/lib/api/form'
import { cn } from '@/lib/utils'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { Field } from '@/components/shared/field'
import { FormField } from '@/components/shared/form-field'
import { StatusPill } from '@/components/shared/status-pill'
import { Markdown } from '@/components/shared/markdown'
import { TagContext } from '@/components/shared/tag-context'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { PageActions } from '@/components/shared/page-actions'
import { Modal } from '@/components/ui/modal'
import { Textarea } from '@/components/ui/textarea'
import { useEffectivePermissions, usePermissions } from '@/lib/auth/use-permissions'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { useEvaluationGroup } from '@/features/evaluation-groups/queries'
import type { MessageFlagResponse } from '@/lib/api/types'

const editSchema = z.object({
  reason: z.string().min(1, 'Required'),
  comment: z.string(),
  red_flagged: z.boolean(),
})
type EditFormValues = z.infer<typeof editSchema>

function EditFlagDialog({
  flag,
  open,
  onOpenChange,
}: {
  flag: MessageFlagResponse
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const update = useUpdateFlag(flag.id)
  const initial: EditFormValues = {
    reason: flag.reason,
    comment: flag.comment ?? '',
    red_flagged: flag.red_flagged,
  }
  const { register, handleSubmit, reset, setError, formState } = useForm<EditFormValues>({
    resolver: zodResolver(editSchema),
    defaultValues: initial,
  })

  const onSubmit = handleSubmit(async (values) => {
    try {
      await update.mutateAsync({
        reason: values.reason.trim(),
        comment: values.comment || null,
        red_flagged: values.red_flagged,
      })
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Edit flag" onOpen={() => reset(initial)}>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Reason" htmlFor="edit-reason" error={formState.errors.reason?.message}>
          <Textarea id="edit-reason" rows={3} {...register('reason')} />
        </FormField>
        <FormField
          label="Comment (optional)"
          htmlFor="edit-comment"
          error={formState.errors.comment?.message}
        >
          <Textarea id="edit-comment" rows={2} {...register('comment')} />
        </FormField>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 size-4 rounded border"
            {...register('red_flagged')}
          />
          <span>
            Exploit-worthy
            <span className="text-muted-foreground block text-xs">
              The model was actually broken here.
            </span>
          </span>
        </label>
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={formState.isSubmitting}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting}>
            {formState.isSubmitting ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export function MessageFlagDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const lookup = useUserLookup()
  const query = useMessageFlag(id ?? '')
  const flag = query.data
  // Flag mutations accept the permission from a role held on the flag's group as readily as from
  // the JWT, so the affordances resolve against the same union the server authorizes. The review
  // routes do **not** — they are JWT-only, so `reviews:*` stays on the global checker: asking the
  // union would render a reviews panel whose query never runs, and it would stay "Loading…".
  const parentGroup = useEvaluationGroup(flag?.evaluation_group_id ?? '')
  const { has } = useEffectivePermissions(parentGroup.data?.user_permissions)
  const { has: hasGlobal } = usePermissions()
  const reviews = useFlagReviews(id ?? '')
  const del = useDeleteFlag()
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

  const cameFromApp = location.key !== 'default'

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => (cameFromApp ? navigate(-1) : navigate('/message-flags'))}
      >
        <ArrowLeft className="size-4" /> {cameFromApp ? 'Back' : 'Back to My flags'}
      </Button>

      {query.isPending && <DetailSkeleton />}
      {/* The route is coarse, so a caller with no authority over this flag's group is refused by the
          fetch rather than by the guard. Answer that as a refusal, like the conversation routes do —
          an error panel would read as the flag being broken. Any other failure stays an error. */}
      {query.isError &&
        (isForbidden(query.error) ? (
          <NotAuthorized />
        ) : (
          <p className="text-destructive">{humanizeError(query.error)}</p>
        ))}

      {flag && (
        <>
          <Breadcrumbs items={[{ label: 'My flags', to: '/message-flags' }, { label: 'Flag' }]} />

          <header className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-3">
                  <h1 className="font-display text-2xl font-semibold tracking-tight">
                    {flag.reason}
                  </h1>
                  <StatusPill status={flag.status} />
                  {flag.red_flagged && <StatusPill status="red_flagged" />}
                </div>
                <p className="text-muted-foreground font-mono text-xs">
                  flagged {new Date(flag.created_at).toLocaleString()}
                </p>
                {flag.comment && <p className="text-muted-foreground max-w-2xl">{flag.comment}</p>}
              </div>
              <PageActions
                secondary={[
                  {
                    key: 'review-queue',
                    label: 'Open in review queue',
                    icon: ExternalLink,
                    onSelect: () => navigate(`/reviews/submissions/${flag.id}`),
                    when: hasGlobal('reviews:update'),
                  },
                  {
                    key: 'edit',
                    label: 'Edit',
                    icon: Pencil,
                    onSelect: () => setEditOpen(true),
                    when: has('flags:update'),
                  },
                  {
                    key: 'delete',
                    label: 'Delete',
                    icon: Trash2,
                    onSelect: () => setDeleteOpen(true),
                    destructive: true,
                    when: has('flags:delete'),
                  },
                ]}
              />
            </div>
          </header>

          <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-3">
            <div className="min-w-0 space-y-3 lg:col-span-2">
              <div className="flex items-center justify-between">
                <h2 className="font-display text-lg font-semibold tracking-tight">
                  Flagged messages ({flag.messages.length})
                </h2>
                <Link
                  to={`/evaluations/${flag.evaluation_id}/conversations/${flag.conversation_id}`}
                  className="text-muted-foreground hover:text-foreground text-sm underline-offset-2 hover:underline"
                >
                  View full conversation
                </Link>
              </div>
              {flag.messages.map((m) => (
                <div
                  key={m.id}
                  className={cn(
                    'rounded-md border px-3 py-2',
                    m.role === 'user' ? 'bg-muted/40' : 'bg-card',
                  )}
                >
                  <div className="text-muted-foreground mb-1 text-xs font-medium tracking-wide">
                    {m.role}
                  </div>
                  <Markdown content={m.content} />
                  <TagContext
                    tagContext={m.tag_context}
                    partial={m.tag_context_partial}
                    messageId={m.id}
                  />
                </div>
              ))}
            </div>

            <aside className="min-w-0 space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle>Outcome</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  {!hasGlobal('reviews:read') ? (
                    <Field label="Status">
                      <StatusPill status={flag.status} />
                    </Field>
                  ) : reviews.isPending ? (
                    <p className="text-muted-foreground text-sm">Loading reviews…</p>
                  ) : (reviews.data?.items.length ?? 0) === 0 ? (
                    <p className="text-muted-foreground text-sm">
                      Not reviewed yet. Reviews appear once a reviewer is assigned.
                    </p>
                  ) : (
                    reviews.data?.items.map((r) => (
                      <div key={r.id} className="space-y-1 border-b pb-2 last:border-0 last:pb-0">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-sm">{lookup(r.reviewer_id)}</span>
                          <StatusPill status={r.status} />
                        </div>
                        {r.status !== 'pending' && (
                          <p className="text-muted-foreground text-xs">{verdictSummary(r)}</p>
                        )}
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Details</CardTitle>
                </CardHeader>
                <CardContent>
                  <dl className="space-y-3">
                    <Field label="Created by">{lookup(flag.created_by_id)}</Field>
                    <Field label="Exploit-worthy">{flag.red_flagged ? 'Yes' : 'No'}</Field>
                    <Field label="Flagged">{new Date(flag.created_at).toLocaleString()}</Field>
                  </dl>
                </CardContent>
              </Card>
            </aside>
          </div>

          <EditFlagDialog flag={flag} open={editOpen} onOpenChange={setEditOpen} />

          <ConfirmDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            title="Delete flag"
            description="Remove this flag? Reviews already recorded on it are kept by the backend."
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() => del.mutate(flag.id, { onSuccess: () => navigate('/message-flags') })}
          />
        </>
      )}
    </div>
  )
}
