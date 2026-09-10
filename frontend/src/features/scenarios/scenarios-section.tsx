import { useState } from 'react'
import { ChevronDown, ChevronUp, MessageSquarePlus, Pencil, Plus, Trash2 } from 'lucide-react'
import { useEvaluationScenarios, useScenarioTasks } from './queries'
import { useDeleteScenario, useDeleteTask, useReorderScenarios } from './mutations'
import { ScenarioFormDialog } from './scenario-form-dialog'
import { TaskFormDialog } from './task-form-dialog'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

function TasksSubsection({
  scenario,
  canMutate,
}: {
  scenario: ScenarioResponse
  canMutate: boolean
}) {
  const list = useScenarioTasks(scenario.id)
  const del = useDeleteTask(scenario.id)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<TaskResponse | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TaskResponse | null>(null)

  const tasks = list.data?.items ?? []

  return (
    <div className="mt-2 ml-5 space-y-1 border-l pl-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-muted-foreground text-xs font-medium">Tasks ({tasks.length})</span>
        {canMutate && (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-xs"
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            <Plus className="size-3" /> Add task
          </Button>
        )}
      </div>

      {tasks.map((t) => (
        <div key={t.id} className="flex items-start gap-2 rounded px-2 py-1 text-sm">
          <div className="min-w-0 flex-1">
            <span className="font-medium">{t.name}</span>
            {t.description && (
              <p className="text-muted-foreground truncate text-xs">{t.description}</p>
            )}
          </div>
          {canMutate && (
            <div className="flex shrink-0 gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="size-6"
                aria-label="Edit task"
                onClick={() => {
                  setEditing(t)
                  setFormOpen(true)
                }}
              >
                <Pencil className="size-3" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="size-6"
                aria-label="Delete task"
                onClick={() => setDeleteTarget(t)}
              >
                <Trash2 className="size-3" />
              </Button>
            </div>
          )}
        </div>
      ))}

      {!list.isPending && tasks.length === 0 && (
        <p className="text-muted-foreground text-xs">
          No tasks yet. Add tasks to spell out what this scenario asks red-teamers to attempt.
        </p>
      )}

      {canMutate && (
        <>
          <TaskFormDialog
            scenarioId={scenario.id}
            open={formOpen}
            onOpenChange={setFormOpen}
            task={editing}
          />
          <ConfirmDialog
            open={deleteTarget !== null}
            onOpenChange={(o) => {
              if (!o) setDeleteTarget(null)
            }}
            title="Delete task"
            description={
              // Not `REVERSIBLE_DELETE_NOTE`: tasks have no browsable tombstone surface, so
              // the only way back is the Undo on the toast that follows. Promising a
              // limited-time window here would outlive the toast that has to deliver it.
              deleteTarget
                ? `Delete "${deleteTarget.name}"? You can undo this from the confirmation that follows.`
                : undefined
            }
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() =>
              deleteTarget &&
              del.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) })
            }
          />
        </>
      )}
    </div>
  )
}

export function ScenariosSection({
  evaluationId,
  onStartConversation,
}: {
  evaluationId: string
  onStartConversation?: (scenarioId: string) => void
}) {
  const list = useEvaluationScenarios(evaluationId)
  const del = useDeleteScenario(evaluationId)
  const reorder = useReorderScenarios(evaluationId)
  const { has } = usePermissions()
  const canMutate = has('evaluations:update')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ScenarioResponse | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ScenarioResponse | null>(null)

  const scenarios = [...(list.data?.items ?? [])].sort((a, b) => a.position - b.position)

  const move = (index: number, dir: -1 | 1) => {
    const j = index + dir
    if (j < 0 || j >= scenarios.length) return
    const ids = scenarios.map((s) => s.id)
    const a = ids[index]
    const b = ids[j]
    if (a === undefined || b === undefined) return
    ids[index] = b
    ids[j] = a
    reorder.mutate(ids)
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Scenarios ({scenarios.length})</h2>
        {canMutate && (
          <Button
            size="sm"
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            <Plus className="size-4" /> Add scenario
          </Button>
        )}
      </div>

      {list.isError && <p className="text-destructive">Error: {(list.error as Error).message}</p>}

      <div className="space-y-2">
        {scenarios.map((s, i) => (
          <div key={s.id} className="bg-card rounded-md border px-3 py-2">
            {/* Wraps: the trailing controls are all `shrink-0` and together overflowed the row. */}
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-muted-foreground w-5 text-xs">{s.position}</span>
              <div className="min-w-0 flex-1">
                <div className="font-medium">{s.name}</div>
                <div className="text-muted-foreground truncate text-sm">{s.description}</div>
              </div>
              <span className="text-muted-foreground shrink-0 text-xs">
                {s.required_reviews} reviewer(s)
              </span>
              {onStartConversation && (
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={() => onStartConversation(s.id)}
                  title="Start a conversation with this scenario"
                >
                  <MessageSquarePlus className="size-4" /> Start
                </Button>
              )}
              {canMutate && (
                <div className="flex shrink-0 gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Move scenario up"
                    disabled={i === 0 || reorder.isPending}
                    onClick={() => move(i, -1)}
                  >
                    <ChevronUp className="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Move scenario down"
                    disabled={i === scenarios.length - 1 || reorder.isPending}
                    onClick={() => move(i, 1)}
                  >
                    <ChevronDown className="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Edit scenario"
                    onClick={() => {
                      setEditing(s)
                      setFormOpen(true)
                    }}
                  >
                    <Pencil className="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Delete scenario"
                    onClick={() => setDeleteTarget(s)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              )}
            </div>
            <TasksSubsection scenario={s} canMutate={canMutate} />
          </div>
        ))}
        {!list.isPending && scenarios.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No scenarios yet. Add a scenario to give red-teamers a challenge to probe.
          </p>
        )}
      </div>

      <ScenarioFormDialog
        evaluationId={evaluationId}
        open={formOpen}
        onOpenChange={setFormOpen}
        scenario={editing}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeleteTarget(null)
        }}
        title="Delete scenario"
        description={
          // Same as the task dialog above: Undo on the following toast is the only way back.
          deleteTarget
            ? `Delete "${deleteTarget.name}"? You can undo this from the confirmation that follows.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          deleteTarget && del.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) })
        }
      />
    </div>
  )
}
