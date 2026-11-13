import { useId, useState } from 'react'
import { ChevronDown, ChevronUp, Pencil, Plus, Trash2 } from 'lucide-react'
import { useEvaluationScenarios, useScenarioTasks } from './queries'
import { useDeleteScenario, useDeleteTask, useReorderScenarios } from './mutations'
import { ScenarioFormDialog } from './scenario-form-dialog'
import { TaskFormDialog } from './task-form-dialog'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Field } from '@/components/shared/field'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePermissions } from '@/lib/auth/use-permissions'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

const formatDate = (value: string) =>
  new Date(value).toLocaleDateString(undefined, { dateStyle: 'long' })

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
    <div className="space-y-2">
      <p className="text-muted-foreground text-xs font-medium tracking-wide">
        Tasks ({tasks.length})
      </p>
      <ul role="list" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {tasks.map((t, i) => (
          <li key={t.id} className="bg-background rounded-md border p-3">
            <div className="flex items-start justify-between gap-2">
              <p className="min-w-0 text-sm break-words">
                <span className="font-medium">Task {i + 1}:</span> {t.name}
              </p>
              {canMutate && (
                <div className="-mt-1 -mr-1 flex shrink-0">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label="Edit task"
                    onClick={() => {
                      setEditing(t)
                      setFormOpen(true)
                    }}
                  >
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label="Delete task"
                    onClick={() => setDeleteTarget(t)}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              )}
            </div>
            {t.description && (
              <p className="text-muted-foreground mt-2 text-sm break-words whitespace-pre-line">
                {t.description}
              </p>
            )}
          </li>
        ))}
        {canMutate && (
          <li className="flex items-center">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setEditing(null)
                setFormOpen(true)
              }}
            >
              <Plus className="size-4" /> Add task
            </Button>
          </li>
        )}
      </ul>

      {!list.isPending && tasks.length === 0 && (
        <p className="text-muted-foreground text-sm">
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

export function ScenariosSection({ evaluationId }: { evaluationId: string }) {
  const list = useEvaluationScenarios(evaluationId)
  const del = useDeleteScenario(evaluationId)
  const reorder = useReorderScenarios(evaluationId)
  const { has } = usePermissions()
  const canMutate = has('evaluations:update')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ScenarioResponse | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ScenarioResponse | null>(null)
  const headingId = useId()

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
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="font-display text-lg font-semibold tracking-tight">
        Evaluation content — scenarios and tasks
      </h2>

      {list.isError && <p className="text-destructive">Error: {(list.error as Error).message}</p>}

      <div className="space-y-4">
        {scenarios.map((s, i) => (
          <Card key={s.id}>
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <CardTitle className="min-w-0 break-words">
                  <span className="text-muted-foreground">Scenario {i + 1}:</span> {s.name}
                </CardTitle>
                {canMutate && (
                  <div className="-mt-1.5 flex shrink-0">
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
            </CardHeader>
            <CardContent className="space-y-4">
              <dl className="grid gap-4 sm:grid-cols-3">
                <Field label="Date added">{formatDate(s.created_at)}</Field>
                {/* Reviewer consensus is set up by whoever runs the evaluation, not a red teamer's concern. */}
                {canMutate && <Field label="Required reviewers">{s.required_reviews}</Field>}
                {s.description && (
                  <Field label="Scenario description" className="sm:col-span-3">
                    <span className="whitespace-pre-line">{s.description}</span>
                  </Field>
                )}
              </dl>
              <TasksSubsection scenario={s} canMutate={canMutate} />
            </CardContent>
          </Card>
        ))}
        {!list.isPending && scenarios.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No scenarios yet. Add a scenario to give red-teamers a challenge to probe.
          </p>
        )}
      </div>

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
    </section>
  )
}
