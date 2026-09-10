import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type {
  ScenarioCreate,
  ScenarioResponse,
  ScenarioUpdate,
  TaskCreate,
  TaskUpdate,
} from '@/lib/api/types'

type ScenarioPage = { items: ScenarioResponse[]; total: number; limit: number; offset: number }

function useInvalidateScenarios(evaluationId: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['evaluation-scenarios', evaluationId] })
    qc.invalidateQueries({ queryKey: ['scenarios'] })
    // The group detail's publication_blockers are derived from the scenario set, so a
    // scenario write can be what unblocks Publish — a stale group page keeps it disabled.
    qc.invalidateQueries({ queryKey: ['evaluation-group'] })
  }
}

export function useCreateScenario(evaluationId: string) {
  const invalidate = useInvalidateScenarios(evaluationId)
  return useMutation({
    mutationFn: async (body: ScenarioCreate) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/scenarios', {
          params: { path: { evaluation_id: evaluationId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Scenario created')
    },
  })
}

export function useUpdateScenario(evaluationId: string) {
  const invalidate = useInvalidateScenarios(evaluationId)
  return useMutation({
    mutationFn: async ({ scenarioId, body }: { scenarioId: string; body: ScenarioUpdate }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluations/{evaluation_id}/scenarios/{scenario_id}', {
          params: { path: { evaluation_id: evaluationId, scenario_id: scenarioId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Scenario updated')
    },
  })
}

export function useRestoreScenario(evaluationId: string) {
  const invalidate = useInvalidateScenarios(evaluationId)
  return useMutation({
    mutationFn: async (scenarioId: string) =>
      unwrap(
        await apiClient.POST(
          '/api/v1/evaluations/{evaluation_id}/scenarios/{scenario_id}/restore',
          { params: { path: { evaluation_id: evaluationId, scenario_id: scenarioId } } },
        ),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Scenario restored')
    },
  })
}

export function useDeleteScenario(evaluationId: string) {
  const invalidate = useInvalidateScenarios(evaluationId)
  const restore = useRestoreScenario(evaluationId)
  return useMutation({
    mutationFn: async (scenarioId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/evaluations/{evaluation_id}/scenarios/{scenario_id}', {
          params: { path: { evaluation_id: evaluationId, scenario_id: scenarioId } },
        }),
      ),
    onSuccess: (_data, scenarioId) => {
      invalidate()
      let undone = false
      toast.success('Scenario deleted', {
        // Scenarios have no list surface of their own in the console, so Undo is the only
        // way back — the same shape saved views ended up with.
        description: 'Undo here is the only way back in the console.',
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(scenarioId)
          },
        },
      })
    },
  })
}

function useInvalidateTasks(scenarioId: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['scenario-tasks', scenarioId] })
  }
}

export function useCreateTask(scenarioId: string) {
  const invalidate = useInvalidateTasks(scenarioId)
  return useMutation({
    mutationFn: async (body: TaskCreate) =>
      unwrap(
        await apiClient.POST('/api/v1/scenarios/{scenario_id}/tasks', {
          params: { path: { scenario_id: scenarioId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Task created')
    },
  })
}

export function useUpdateTask(scenarioId: string) {
  const invalidate = useInvalidateTasks(scenarioId)
  return useMutation({
    mutationFn: async ({ taskId, body }: { taskId: string; body: TaskUpdate }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/scenarios/{scenario_id}/tasks/{task_id}', {
          params: { path: { scenario_id: scenarioId, task_id: taskId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Task updated')
    },
  })
}

export function useRestoreTask(scenarioId: string) {
  const invalidate = useInvalidateTasks(scenarioId)
  return useMutation({
    mutationFn: async (taskId: string) =>
      unwrap(
        await apiClient.POST('/api/v1/scenarios/{scenario_id}/tasks/{task_id}/restore', {
          params: { path: { scenario_id: scenarioId, task_id: taskId } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Task restored')
    },
  })
}

export function useDeleteTask(scenarioId: string) {
  const invalidate = useInvalidateTasks(scenarioId)
  const restore = useRestoreTask(scenarioId)
  return useMutation({
    mutationFn: async (taskId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/scenarios/{scenario_id}/tasks/{task_id}', {
          params: { path: { scenario_id: scenarioId, task_id: taskId } },
        }),
      ),
    onSuccess: (_data, taskId) => {
      invalidate()
      let undone = false
      toast.success('Task deleted', {
        // Restoring keeps the per-participant completions the task already collected;
        // re-creating it would not.
        description: 'Undo here is the only way back in the console.',
        action: {
          label: 'Undo',
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(taskId)
          },
        },
      })
    },
  })
}

export function useReorderScenarios(evaluationId: string) {
  const qc = useQueryClient()
  const invalidate = useInvalidateScenarios(evaluationId)
  const key = ['evaluation-scenarios', evaluationId] as const
  return useMutation({
    mutationFn: async (scenario_ids: string[]) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluations/{evaluation_id}/scenarios/order', {
          params: { path: { evaluation_id: evaluationId } },
          body: { scenario_ids },
        }),
      ),
    onMutate: async (scenario_ids) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<ScenarioPage>(key)
      if (prev) {
        const byId = new Map(prev.items.map((s) => [s.id, s]))
        const reordered = scenario_ids.flatMap((id) => {
          const s = byId.get(id)
          return s ? [s] : []
        })
        qc.setQueryData<ScenarioPage>(key, { ...prev, items: reordered })
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData<ScenarioPage>(key, ctx.prev)
    },
    onSettled: () => invalidate(),
  })
}
