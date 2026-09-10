import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Ban as BanIcon, Maximize2 } from 'lucide-react'
import { useMessages } from './queries'
import type { ConversationResponse } from '@/lib/api/types'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import { humanizeError } from '@/lib/api/problem'
import { useConversationStream } from './use-conversation-stream'
import { useWarmupGate } from './use-model-warmup'
import { WarmupBadge } from './warmup-badge'
import { Badge } from '@/components/ui/badge'
import { Bubble } from './bubble'
import { ChatComposer } from './chat-composer'
import type { AllowedKeysStatus } from './tag-key-field'
import { unsentTagKeys } from './tag-rows'
import { memberA11yLabel, memberLabel } from './member-names'
import { AssistantActions } from './assistant-actions'
import { LabelsTruncatedNote } from '@/features/annotations/labels-truncated-note'
import { AnnotateMessageDialog } from '@/features/annotations/annotate-message-dialog'
import { indexAnnotations } from '@/features/annotations/annotation-index'
import { useConversationAnnotations } from '@/features/annotations/queries'
import { FlagMessageDialog } from '@/features/message-flags/flag-message-dialog'

export function ConversationPane({
  evaluationId,
  conversationId,
  assignmentId,
  modelName,
  nameSuffix,
  warmupEnabled,
  acceptsImages,
  tagsEnabled,
  allowedTagKeys,
  allowedKeysStatus,
  tags,
  onWarmupChange,
  title,
  prompt,
  promptNonce,
  groupPermissions,
  canWrite,
  canFlag,
}: {
  evaluationId: string
  conversationId: string
  assignmentId: string
  modelName: string
  // ` (n)` when this pane's name isn't unique in the group, else empty (see member-names).
  nameSuffix?: string
  warmupEnabled: boolean
  acceptsImages: boolean
  // Authoritative here, unlike on the full conversation page: the group page renders a pane only once
  // the evaluation has resolved, so `false` means "off", never "not known yet" — which is what lets the
  // marker and the row's name state it.
  tagsEnabled: boolean
  // The evaluation's allowed message-tag keys when it restricts tags, else `null` (free-form).
  allowedTagKeys: string[] | null
  allowedKeysStatus: AllowedKeysStatus
  // The conversation's own tags (the model-facing context), shown read-only here — editing
  // them lives on the full conversation page.
  tags?: ConversationResponse['tags']
  onWarmupChange?: (conversationId: string, blocked: boolean) => void
  title?: string | null
  prompt: string
  promptNonce: number
  // The parent group's `user_permissions`, threaded so the transcript query applies the same
  // global-or-in-group rule as the page: the caller's global set alone answers wrong for a
  // member whose authority is in-group.
  groupPermissions?: readonly string[]
  // Whether this caller may write to the group at all. `read_any` surfaces other members' groups
  // read-only, and a pane is a write surface of its own — the shared broadcast being hidden says
  // nothing about the N per-pane composers.
  canWrite: boolean
  // Narrower than `canWrite`: flag authoring is owner-only on the backend *even* for the
  // `evaluation_groups:manage` break-glass, so the group page resolves it per member.
  canFlag: boolean
}) {
  const label = memberLabel({ title, modelName }, nameSuffix)
  const a11yLabel = memberA11yLabel({ title, modelName }, nameSuffix)
  const messages = useMessages(evaluationId, conversationId, { groupPermissions })
  const stream = useConversationStream(evaluationId, conversationId)
  const { user } = useAuth()
  // Not the group-aware `allows` the other gates use: the annotations routes read the JWT's
  // global claim, so an in-group annotator would be offered the affordance and refused with a
  // 403 — the same rule the full conversation page follows.
  const canAnnotate = usePermissions().has('annotations:create')
  // Self-gated on `annotations:read`, so a red teamer holding no annotation key runs no query
  // here and sees no chips.
  const annotationsQ = useConversationAnnotations(conversationId)
  const [flagIds, setFlagIds] = useState<string[] | null>(null)
  const [annotateId, setAnnotateId] = useState<string | null>(null)
  // Warming is a write on the warmup route, so it follows `canWrite` — see the conversation page.
  const warmup = useWarmupGate(evaluationId, assignmentId, warmupEnabled && canWrite)
  const lastSentNonce = useRef<number>(-1)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (promptNonce <= 0 || !prompt || lastSentNonce.current === promptNonce) return
    lastSentNonce.current = promptNonce
    // A model that's still warming can't take the message — skip the broadcast for it
    // (its own composer is blocked too); the badge shows why.
    if (warmup.blocked) return
    void stream.send(prompt)
  }, [prompt, promptNonce, stream, warmup.blocked])

  // Report this pane's readiness up so the group can block the broadcast while any
  // model warms; clear the report on unmount so a removed pane leaves no stale block.
  useEffect(() => {
    onWarmupChange?.(conversationId, warmup.blocked)
  }, [conversationId, warmup.blocked, onWarmupChange])
  useEffect(() => () => onWarmupChange?.(conversationId, false), [conversationId, onWarmupChange])

  const sorted = [...(messages.data?.items ?? [])].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  )
  // A turn's reply record is proof of what was actually sent — reconciled into each message's
  // marker set below, in place of the policy's guess, for any key it carries.
  const recordedByTurn = new Map<string, Record<string, string>>()
  for (const m of sorted) {
    if (m.role === 'assistant' && m.tag_context && Object.keys(m.tag_context).length > 0) {
      recordedByTurn.set(m.turn_id, m.tag_context)
    }
  }
  const lastAssistant = [...sorted].reverse().find((m) => m.role === 'assistant')
  const annotationIndex = indexAnnotations(
    annotationsQ.data?.items ?? [],
    sorted.map((m) => m.id),
  )
  const tagEntries = Object.entries(tags ?? {})
  // Same rules as the server's prompt fold, so a filtered-out tag isn't shown as model context.
  // These chips describe what the *next* turn will send under today's policy; a bubble's own chips
  // describe what its turn did send, so the same key can differ between the two.
  const unsentTags = unsentTagKeys(tags ?? {}, {
    tagsEnabled,
    allowedKeys: allowedTagKeys,
    keysSettled: allowedKeysStatus === 'success',
  })

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: 'end' })
  }, [sorted.length, stream.streaming?.assistantText, stream.streaming?.userText])

  return (
    <div className="bg-card flex h-[calc(100svh-17rem)] min-h-80 min-w-[340px] flex-1 flex-col rounded-lg border">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold">{label}</span>
          {warmupEnabled && canWrite && (
            <WarmupBadge state={warmup.state} onRetry={warmup.warmUp} />
          )}
          {title && (
            <span className="text-muted-foreground block truncate text-xs">{modelName}</span>
          )}
          {/* In the header, not the transcript body below: the body is auto-scrolled to the newest
              message, so on the busy transcript this warns about it would never be read. */}
          <LabelsTruncatedNote total={annotationsQ.data?.total} />
          {/* Rendered whatever the flag says — see the same chips on the full conversation page. */}
          {tagEntries.length > 0 && (
            // `role="list"` spelled out, as on the full conversation page: naming is not allowed on a
            // generic `<div>`, so an `aria-label` there reaches no screen reader at all — the chips
            // read as loose text in the pane header. The reset also drops list semantics in Safari.
            <ul
              role="list"
              className="mt-1 flex flex-wrap gap-1"
              data-testid="pane-conversation-tags"
              // Named per pane, like the composer's `a11yLabel`: side by side, several rows all called
              // "Tags" are indistinguishable in a screen reader's element list. A `title` here would
              // be shadowed by each chip's own anyway, so the reason lives in the name and, per chip,
              // in the marker below.
              aria-label={
                tagsEnabled
                  ? `Tags for ${a11yLabel}`
                  : `Tags for ${a11yLabel} — kept but not sent to the model, tagging is off for this evaluation`
              }
            >
              {tagEntries.map(([k, v]) => (
                <li key={k}>
                  {/* Capped like the full page's chips (narrower — a pane header is): an uncapped
                      512-char value wraps to four lines and takes the transcript's space with it. */}
                  <Badge
                    variant="tag"
                    className="max-w-[14rem]"
                    // The reason rides the `title` here too: a pane header has no room for the full page's
                    // inline copy, so without it a sighted mouse user sees a bare glyph.
                    title={unsentTags.has(k) ? `${k}: ${v} — not sent to the model` : `${k}: ${v}`}
                  >
                    <span className="truncate">
                      {k}: {v}
                    </span>
                    {/* Marked, not dimmed — see the full page: `opacity-60` measured 2.35:1 in the light
                        theme, and it is invisible to a reader either way. This header has no room for
                        the full page's inline copy, so the reason rides the `title` above. */}
                    {unsentTags.has(k) && (
                      <>
                        <BanIcon className="ml-1 size-3 shrink-0" aria-hidden />
                        <span className="sr-only">(not sent to the model)</span>
                      </>
                    )}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </div>
        <Link
          to={`/evaluations/${evaluationId}/conversations/${conversationId}`}
          aria-label={`Open full conversation with ${a11yLabel}`}
          title="Open full conversation"
          className="text-muted-foreground hover:text-foreground shrink-0"
        >
          <Maximize2 className="size-4" />
        </Link>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {messages.isPending && <p className="text-muted-foreground text-sm">Loading…</p>}
        {annotationsQ.isError && (
          <p className="text-destructive text-xs">
            Labels are unavailable: {humanizeError(annotationsQ.error)}
          </p>
        )}
        {sorted.map((m) => {
          const messageUnsent = unsentTagKeys(m.tags ?? {}, {
            tagsEnabled,
            allowedKeys: allowedTagKeys,
            keysSettled: allowedKeysStatus === 'success',
          })
          const record = recordedByTurn.get(m.turn_id)
          // A non-empty record decides both ways: what it carries was sent, what it omits was not. The
          // policy guess only survives where the turn has no record at all. Raw keys compare equal to the
          // backend's sanitised comparison (`TagFoldPolicy.unsent_for_message`), because a tag key is
          // charset-validated (`TAG_KEY_PATTERN`) to characters sanitising never touches.
          const unsentForMessage = record
            ? new Set(Object.keys(m.tags ?? {}).filter((key) => !(key in record)))
            : messageUnsent
          return (
            <Bubble
              key={m.id}
              role={m.role}
              content={m.content}
              status={m.status}
              flagCount={m.flag_count}
              imageKeys={m.image_keys}
              messageId={m.id}
              tagContext={m.tag_context}
              tagContextPartial={m.tag_context_partial}
              tags={m.tags}
              unsentTags={unsentForMessage}
              onFlag={m.role === 'assistant' && canFlag ? () => setFlagIds([m.id]) : undefined}
              annotations={annotationIndex.byMessage.get(m.id) ?? []}
              onAnnotate={canAnnotate ? () => setAnnotateId(m.id) : undefined}
            />
          )
        })}

        {stream.streaming?.userText !== undefined && (
          <Bubble
            role="user"
            content={stream.streaming.userText}
            imageKeys={stream.streaming.imageKeys}
          />
        )}
        {stream.streaming && (
          <Bubble role="assistant" content={stream.streaming.assistantText} status="streaming" />
        )}

        {messages.data && sorted.length === 0 && !stream.streaming && (
          <p className="text-muted-foreground text-sm">No messages yet.</p>
        )}
        <div ref={endRef} />
      </div>

      {/* Per-pane composer: message just this model, independent of the shared broadcast
          (attachments are per-pane too — the broadcast stays text-only). */}
      {canWrite && (
        <div className="space-y-1.5 border-t p-2">
          {lastAssistant && !stream.streaming && (
            <AssistantActions
              onRegenerate={() => stream.regenerate(lastAssistant.id)}
              onContinue={() => stream.continueReply(lastAssistant.id)}
              disabled={stream.pending || warmup.blocked}
            />
          )}
          <ChatComposer
            send={stream.send}
            pending={stream.pending}
            disabled={warmup.blocked}
            acceptsImages={acceptsImages}
            tagsEnabled={tagsEnabled}
            allowedTagKeys={allowedTagKeys}
            allowedKeysStatus={allowedKeysStatus}
            placeholder={
              warmup.blocked ? 'Waiting for the model to warm up…' : `Message ${label} only…`
            }
            ariaLabel={`Message ${a11yLabel}`}
            rows={1}
            size="sm"
          />
        </div>
      )}

      <FlagMessageDialog
        conversationId={conversationId}
        messageIds={flagIds ?? []}
        open={flagIds !== null}
        onOpenChange={(o) => {
          if (!o) setFlagIds(null)
        }}
      />

      <AnnotateMessageDialog
        messageId={annotateId ?? ''}
        conversationId={conversationId}
        annotations={annotationIndex.byMessage.get(annotateId ?? '') ?? []}
        currentUserId={user?.id}
        open={annotateId !== null}
        onOpenChange={(o) => {
          if (!o) setAnnotateId(null)
        }}
      />
    </div>
  )
}
