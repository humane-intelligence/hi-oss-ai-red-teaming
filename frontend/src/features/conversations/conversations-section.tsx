import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, useNavigate } from 'react-router-dom'
import {
  ChevronDown,
  ChevronRight,
  Columns2,
  MessageSquare,
  Pencil,
  Plus,
  Trash2,
} from 'lucide-react'
import { useEffectivePermissions, usePermissions } from '@/lib/auth/use-permissions'
import { useAuth } from '@/lib/auth/auth-context'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { useConversationGroups, useDeletedConversations } from './queries'
import {
  useDeleteConversationGroup,
  useRenameConversationGroup,
  useRestoreConversation,
} from './mutations'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import type {
  ConversationGroupResponse,
  ConversationResponse,
  EvaluationResponse,
} from '@/lib/api/types'

export function ConversationsSection({
  evaluation,
  groupPermissions,
}: {
  evaluation: EvaluationResponse
  // The caller's in-group authority on the parent group (`user_permissions` from the group
  // fetch). The server accepts either source, so an in-group `red_teamer` whose global roles
  // grant no conversation permissions still gets the affordances here.
  groupPermissions?: readonly string[]
}) {
  const navigate = useNavigate()
  const { user } = useAuth()
  // The break-glass lift on group PATCH/DELETE is read off the JWT server-side, so it is the
  // global set that decides it — not the group union.
  const canManage = usePermissions().has('evaluation_groups:manage')
  const { has: allows } = useEffectivePermissions(groupPermissions)
  const canRead = allows('conversations:read')
  const groupsQuery = useConversationGroups(evaluation.id, { groupPermissions })
  const groups = groupsQuery.data?.items ?? []
  const models = evaluation.models ?? []
  const canCreate = allows('conversations:create')
  const canRename = allows('conversations:update')
  const canDelete = allows('conversations:delete')
  const total = groups.length

  const [renameTarget, setRenameTarget] = useState<ConversationGroupResponse | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ConversationGroupResponse | null>(null)
  const del = useDeleteConversationGroup(evaluation.id)

  const modelName = (assignmentId: string) =>
    models.find((m) => m.assignment_id === assignmentId)?.name ?? '— masked —'

  // Without the read permission the query never runs, so a rendered section would claim a
  // count of zero for conversations the caller simply cannot see.
  if (!canRead) return null

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display min-w-0 text-lg font-semibold tracking-tight">
          Conversations ({total})
        </h2>
        {canCreate && (
          <Button
            variant="outline"
            size="sm"
            disabled={models.length === 0}
            onClick={() => navigate(`/evaluations/${evaluation.id}/conversation-groups/new`)}
          >
            <Plus className="size-4" /> New conversation
          </Button>
        )}
      </div>

      {models.length === 0 && (
        <p className="text-muted-foreground text-sm">Assign a model to start conversations.</p>
      )}
      {groupsQuery.isError && (
        <p className="text-destructive">Error: {(groupsQuery.error as Error).message}</p>
      )}

      <div className="space-y-2">
        {groups.map((g) => {
          const isOwn = g.user_id === user?.id
          const writable = isOwn || canManage
          return (
            <GroupCard
              key={g.id}
              evaluationId={evaluation.id}
              group={g}
              modelName={modelName}
              onRename={canRename && writable ? () => setRenameTarget(g) : undefined}
              onDelete={canDelete && writable ? () => setDeleteTarget(g) : undefined}
              isOwn={isOwn}
            />
          )
        })}
        {!groupsQuery.isPending && groups.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No conversations yet. Start one to probe a model against this evaluation.
          </p>
        )}
      </div>

      {canDelete && <DeletedConversations evaluationId={evaluation.id} modelName={modelName} />}

      {renameTarget && (
        <RenameGroupDialog
          evaluationId={evaluation.id}
          group={renameTarget}
          open
          onOpenChange={(o) => !o && setRenameTarget(null)}
        />
      )}
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title="Delete conversation"
        description={`Delete this conversation and everything in it? ${REVERSIBLE_DELETE_NOTE}`}
        confirmLabel="Delete"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          deleteTarget &&
          del.mutate(
            {
              groupId: deleteTarget.id,
              conversationIds: (deleteTarget.conversations ?? []).map((c) => c.id),
            },
            { onSuccess: () => setDeleteTarget(null) },
          )
        }
      />
    </div>
  )
}

// Conversations the caller deleted recently, with the one action that reaches them.
// Collapsed by default and only queried once opened — most visits never look, and the
// count itself would cost a request on every render of the evaluation page.
function DeletedConversations({
  evaluationId,
  modelName,
}: {
  evaluationId: string
  modelName: (assignmentId: string) => string
}) {
  const [open, setOpen] = useState(false)
  const query = useDeletedConversations(evaluationId, open)
  const restore = useRestoreConversation(evaluationId)
  const deleted = query.data?.items ?? []

  return (
    <section className="border-t pt-3">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls="deleted-conversations"
        className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-sm font-medium"
      >
        {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        Recently deleted
        {open && !query.isPending && ` (${query.data?.total ?? deleted.length})`}
      </button>
      {open && (
        <div id="deleted-conversations" className="mt-2 space-y-2">
          <p className="text-muted-foreground text-xs">
            Conversations you deleted recently can be restored here for a limited time.
          </p>
          {query.isPending && <p className="text-muted-foreground text-sm">Loading…</p>}
          {query.isError && (
            <p className="text-destructive text-sm">
              Could not load deleted conversations: {humanizeError(query.error)}
            </p>
          )}
          {!query.isPending && !query.isError && deleted.length === 0 && (
            <p className="text-muted-foreground text-sm">Nothing deleted recently.</p>
          )}
          <ul className="space-y-2">
            {deleted.map((conversation: ConversationResponse) => (
              <li
                key={conversation.id}
                className="bg-card flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {conversation.title ?? modelName(conversation.evaluation_ai_model_id)}
                  </p>
                  {conversation.deleted_at && (
                    <p className="text-muted-foreground text-xs">
                      Deleted{' '}
                      <time dateTime={conversation.deleted_at}>
                        {new Date(conversation.deleted_at).toLocaleString()}
                      </time>
                    </p>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Restore conversation: ${conversation.title ?? modelName(conversation.evaluation_ai_model_id)}`}
                  disabled={restore.isPending}
                  onClick={() => restore.mutate(conversation.id)}
                >
                  Restore
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function GroupCard({
  evaluationId,
  group,
  modelName,
  onRename,
  onDelete,
  isOwn,
}: {
  evaluationId: string
  group: ConversationGroupResponse
  modelName: (assignmentId: string) => string
  onRename?: () => void
  onDelete?: () => void
  // `conversations:read_any` puts other members' groups in this list, and a row that shows only a
  // name reads as the caller's own. Only `user_id` is available client-side, so the marker says
  // *not yours* rather than naming the author.
  isOwn: boolean
}) {
  const navigate = useNavigate()
  const members = group.conversations ?? []
  const groupPath = `/evaluations/${evaluationId}/conversation-groups/${group.id}`

  return (
    <div className="bg-card rounded-lg border">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Link to={groupPath} className="min-w-0 truncate font-medium hover:underline">
            {group.name}
          </Link>
          {!isOwn && <Badge variant="tag">Another member's</Badge>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {members.length >= 2 && (
            <Link
              to={groupPath}
              className="hover:bg-accent hover:text-accent-foreground inline-flex h-8 items-center gap-2 rounded-md px-3 text-sm font-medium transition-colors"
            >
              <Columns2 className="size-4" /> Open side-by-side
            </Link>
          )}
          {onRename && (
            <Button variant="ghost" size="icon" aria-label="Rename conversation" onClick={onRename}>
              <Pencil className="size-4" />
            </Button>
          )}
          {onDelete && (
            <Button variant="ghost" size="icon" aria-label="Delete conversation" onClick={onDelete}>
              <Trash2 className="size-4" />
            </Button>
          )}
        </div>
      </div>
      <div className="divide-y">
        {members.map((c) => {
          const label = c.title || modelName(c.evaluation_ai_model_id)
          return (
            <button
              key={c.id}
              onClick={() => navigate(`/evaluations/${evaluationId}/conversations/${c.id}`)}
              className="hover:bg-accent flex w-full items-center gap-3 px-3 py-2 text-left"
            >
              <MessageSquare className="text-muted-foreground size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="font-medium">{label}</div>
                <div className="text-muted-foreground text-xs">
                  {c.title && `${modelName(c.evaluation_ai_model_id)} · `}
                  {new Date(c.created_at).toLocaleString()}
                </div>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

const renameSchema = z.object({ name: z.string().min(1, 'Name is required') })
type RenameFormValues = z.infer<typeof renameSchema>

export function RenameGroupDialog({
  evaluationId,
  group,
  open,
  onOpenChange,
}: {
  evaluationId: string
  group: ConversationGroupResponse
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const rename = useRenameConversationGroup(evaluationId)
  const { register, handleSubmit, reset, setError, formState } = useForm<RenameFormValues>({
    resolver: zodResolver(renameSchema),
    defaultValues: { name: group.name },
  })

  useEffect(() => reset({ name: group.name }), [group.name, reset])

  const onSubmit = handleSubmit(async (values) => {
    try {
      await rename.mutateAsync({ groupId: group.id, name: values.name.trim() })
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Rename conversation">
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormField label="Name" htmlFor="rename-group-name" error={formState.errors.name?.message}>
          <Input id="rename-group-name" autoFocus {...register('name')} />
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
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
