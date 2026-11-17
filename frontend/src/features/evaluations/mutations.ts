import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import type {
  EvaluationAiModelAssign,
  EvaluationAiModelUpdate,
  EvaluationCreate,
  EvaluationLabelCategoryCreate,
  EvaluationLabelCategoryUpdate,
  EvaluationUpdate,
} from '@/lib/api/types'

function useInvalidateEvaluation(id?: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['evaluations'] })
    qc.invalidateQueries({ queryKey: ['evaluation-group'] }) // group detail embeds children
    if (id) qc.invalidateQueries({ queryKey: ['evaluation', id] })
  }
}

export function useCreateEvaluation() {
  const invalidate = useInvalidateEvaluation()
  return useMutation({
    mutationFn: async (body: EvaluationCreate) =>
      unwrap(await apiClient.POST('/api/v1/evaluations', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Evaluation created')
    },
  })
}

// Copy an evaluation into a fresh `new` row in the same group, owned by the caller.
// `includeChildren` also deep-copies its model assignments, scenarios, and tasks.
export function useDuplicateEvaluation() {
  const invalidate = useInvalidateEvaluation()
  return useMutation({
    mutationFn: async ({
      sourceId,
      includeChildren,
    }: {
      sourceId: string
      includeChildren: boolean
    }) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/duplicate', {
          params: {
            path: { evaluation_id: sourceId },
            query: { include_children: includeChildren },
          },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Evaluation duplicated')
    },
  })
}

export function useUpdateEvaluation(id: string) {
  const invalidate = useInvalidateEvaluation(id)
  return useMutation({
    mutationFn: async (body: EvaluationUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluations/{evaluation_id}', {
          params: { path: { evaluation_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Evaluation updated')
    },
  })
}

export function useApproveEvaluation(id: string) {
  const invalidate = useInvalidateEvaluation(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/approve', {
          params: { path: { evaluation_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Evaluation approved')
    },
  })
}

export function useRejectEvaluation(id: string) {
  const invalidate = useInvalidateEvaluation(id)
  return useMutation({
    mutationFn: async (rejection_reason: string) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/reject', {
          params: { path: { evaluation_id: id } },
          body: { rejection_reason },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Evaluation rejected')
    },
  })
}

export function useAssignModel(evaluationId: string) {
  const qc = useQueryClient()
  const invalidate = useInvalidateEvaluation(evaluationId)
  return useMutation({
    mutationFn: async (body: EvaluationAiModelAssign) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/models', {
          params: { path: { evaluation_id: evaluationId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      // The assign picker lists assignable_to_evaluation (subset minus assigned).
      qc.invalidateQueries({ queryKey: ['ai-models'] })
      toast.success('Model assigned')
    },
  })
}

export function useUpdateAssignment(evaluationId: string) {
  const qc = useQueryClient()
  const invalidate = useInvalidateEvaluation(evaluationId)
  return useMutation({
    mutationFn: async ({
      assignmentId,
      body,
    }: {
      assignmentId: string
      body: EvaluationAiModelUpdate
    }) =>
      unwrap(
        await apiClient.PATCH('/api/v1/evaluations/{evaluation_id}/models/{assignment_id}', {
          params: { path: { evaluation_id: evaluationId, assignment_id: assignmentId } },
          body,
        }),
      ),
    onSuccess: (_data, { assignmentId }) => {
      invalidate()
      qc.invalidateQueries({ queryKey: ['assignment', evaluationId, assignmentId] })
      toast.success('Model parameters updated')
    },
  })
}

// Unassigning is the one console delete whose blast radius reaches conversations: the
// backend soft-deletes every conversation run against the assignment and prunes any group
// left empty. Restoring the assignment makes those conversations restorable again (they
// are gated on a live assignment), though each still needs its own restore.
function useInvalidateAssignment(evaluationId: string) {
  const qc = useQueryClient()
  const invalidateEvaluation = useInvalidateEvaluation(evaluationId)
  return () => {
    invalidateEvaluation()
    // Un/re-assigning moves the model in and out of the assignable_to_evaluation picker.
    qc.invalidateQueries({ queryKey: ['ai-models'] })
    qc.invalidateQueries({ queryKey: ['conversation-groups'] })
    qc.invalidateQueries({ queryKey: ['conversation-group'] })
    qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
    // The cascade tombstones the assignment's conversations, and completion sets are read
    // through them.
    qc.invalidateQueries({ queryKey: ['completed-tasks'] })
  }
}

export function useRestoreAssignment(evaluationId: string) {
  const invalidate = useInvalidateAssignment(evaluationId)
  return useMutation({
    mutationFn: async (assignmentId: string) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/models/{assignment_id}/restore', {
          params: { path: { evaluation_id: evaluationId, assignment_id: assignmentId } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Model re-assigned')
    },
  })
}

export function useUnassignModel(evaluationId: string) {
  const invalidate = useInvalidateAssignment(evaluationId)
  const restore = useRestoreAssignment(evaluationId)
  return useMutation({
    mutationFn: async (assignmentId: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/evaluations/{evaluation_id}/models/{assignment_id}', {
          params: { path: { evaluation_id: evaluationId, assignment_id: assignmentId } },
        }),
      ),
    onSuccess: (_data, assignmentId) => {
      invalidate()
      let undone = false
      toast.success('Model unassigned', {
        description: 'Undo here restores the model and makes its conversations restorable again.',
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would re-assign an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(assignmentId)
          },
        },
      })
    },
  })
}

export function useAddTagKey(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (key: string) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/tag-keys', {
          params: { path: { evaluation_id: evaluationId } },
          body: { key },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['evaluation-tag-keys', evaluationId] })
      toast.success('Tag key allowed')
    },
  })
}

export function useRemoveTagKey(evaluationId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (key: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/evaluations/{evaluation_id}/tag-keys/{key}', {
          params: { path: { evaluation_id: evaluationId, key } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['evaluation-tag-keys', evaluationId] })
      toast.success('Tag key removed')
    },
  })
}

// Group labels embed the category name, so a rename or delete also refreshes the conversation groups.
function useInvalidateLabelCategories(evaluationId: string) {
  const qc = useQueryClient()
  // Resolves with the category list only; a slow group refetch must not hold a dialog open.
  return () => {
    void qc.invalidateQueries({ queryKey: ['conversation-groups'] })
    void qc.invalidateQueries({ queryKey: ['conversation-group'] })
    return qc.invalidateQueries({ queryKey: ['evaluation-label-categories', evaluationId] })
  }
}

export function useCreateLabelCategory(evaluationId: string) {
  const invalidate = useInvalidateLabelCategories(evaluationId)
  return useMutation({
    mutationFn: async (body: EvaluationLabelCategoryCreate) =>
      unwrap(
        await apiClient.POST('/api/v1/evaluations/{evaluation_id}/label-categories', {
          params: { path: { evaluation_id: evaluationId } },
          body,
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Label category added')
    },
  })
}

export function useUpdateLabelCategory(evaluationId: string) {
  const invalidate = useInvalidateLabelCategories(evaluationId)
  return useMutation({
    mutationFn: async ({
      categoryId,
      body,
    }: {
      categoryId: string
      body: EvaluationLabelCategoryUpdate
    }) =>
      unwrap(
        await apiClient.PATCH(
          '/api/v1/evaluations/{evaluation_id}/label-categories/{category_id}',
          { params: { path: { evaluation_id: evaluationId, category_id: categoryId } }, body },
        ),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Label category saved')
    },
  })
}

export function useDeleteLabelCategory(evaluationId: string) {
  const invalidate = useInvalidateLabelCategories(evaluationId)
  return useMutation({
    mutationFn: async (categoryId: string) =>
      unwrap(
        await apiClient.DELETE(
          '/api/v1/evaluations/{evaluation_id}/label-categories/{category_id}',
          { params: { path: { evaluation_id: evaluationId, category_id: categoryId } } },
        ),
      ),
    // Returned so the caller's onSuccess sees the refreshed list: below the cap again, Add is enabled.
    onSuccess: () => {
      toast.success('Label category deleted')
      return invalidate()
    },
  })
}
