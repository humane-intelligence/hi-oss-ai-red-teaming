import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { restorableFromHint } from '@/lib/restore/restore'
import type {
  AiModelApiKeyBulkRequest,
  AiModelBulkRequest,
  AiModelCreate,
  AiModelResponse,
  AiModelUpdate,
  BulkModelResponse,
} from '@/lib/api/types'

function useInvalidateModel(id?: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['ai-models'] })
    // The label vocabulary is derived from the rows, so a write can add or retire an entry. Stale
    // suggestions are how near-duplicate spellings get in: the operator doesn't see the label they
    // just used elsewhere and retypes it in another case, which the backend dedupes per payload only.
    qc.invalidateQueries({ queryKey: ['ai-model-labels'] })
    if (id) qc.invalidateQueries({ queryKey: ['ai-model', id] })
  }
}

// Deleting a model reaches well past the registry: it unassigns the model from every
// evaluation and every group's allowed-model subset, and tombstones the conversations
// that ran against those assignments. None of those caches mention models, and the keys
// are not prefixes of each other — `['evaluation-groups']` (the list) does NOT match
// `['evaluation-group', id]` (the detail, which embeds `allowed_models` and the child
// evaluations), so both are needed. Missing the detail one leaves the group edit form
// prefilling a deleted model into `allowed_model_ids`, and saving any unrelated field
// then fails the backend's live-model check with a raw uuid in the message.
function useInvalidateModelCascade(id?: string) {
  const qc = useQueryClient()
  const invalidateModel = useInvalidateModel(id)
  return () => {
    invalidateModel()
    qc.invalidateQueries({ queryKey: ['evaluations'] })
    qc.invalidateQueries({ queryKey: ['evaluation'] })
    qc.invalidateQueries({ queryKey: ['evaluation-groups'] })
    qc.invalidateQueries({ queryKey: ['evaluation-group'] })
    qc.invalidateQueries({ queryKey: ['conversation-groups'] })
    qc.invalidateQueries({ queryKey: ['conversation-group'] })
    qc.invalidateQueries({ queryKey: ['conversations', 'deleted'] })
    // The cascade tombstones the model's conversations, and everything below reads
    // through a live conversation — so these rows leave the server with no cascade
    // rows of their own. `['conversation']` covers the member details the 204 can't name.
    qc.invalidateQueries({ queryKey: ['conversation'] })
    qc.invalidateQueries({ queryKey: ['message-flags'] })
    qc.invalidateQueries({ queryKey: ['notes'] })
    qc.invalidateQueries({ queryKey: ['reviews'] })
    qc.invalidateQueries({ queryKey: ['review-queue'] })
    qc.invalidateQueries({ queryKey: ['completed-tasks'] })
  }
}

// A committed bulk run reports partial success as a warning; a dry-run preview
// commits nothing, so it stays silent and leaves the caches untouched.
function notifyBulk(res: BulkModelResponse, noun: string) {
  const msg = `${res.succeeded} of ${res.total} ${noun}`
  if (res.failed > 0) toast.warning(`${msg}, ${res.failed} failed`)
  else toast.success(msg)
}

export function useCreateModel() {
  const invalidate = useInvalidateModel()
  return useMutation({
    mutationFn: async (body: AiModelCreate) =>
      unwrap(await apiClient.POST('/api/v1/ai-models', { body })),
    onSuccess: () => {
      invalidate()
      toast.success('Model created')
    },
  })
}

export function useUpdateModel(id: string) {
  const qc = useQueryClient()
  const invalidateRegistry = useInvalidateModel(id)
  // An edit — unlike a create or the deliberately shallow restore — changes what
  // `EvaluationAiModelView` projects onto every evaluation that already assigns this model:
  // the name, the warmup flag, the advanced-params opt-out and the effective params derived
  // from it. Those reads live under different keys, so without this a tab sitting on an
  // evaluation keeps the old projection for `staleTime` (30s) and, with no refetch on focus,
  // indefinitely while it stays focused — the New-conversation page would go on offering a
  // parameter panel for a model that had just been set to ignore them.
  const invalidate = () => {
    invalidateRegistry()
    qc.invalidateQueries({ queryKey: ['evaluations'] })
    qc.invalidateQueries({ queryKey: ['evaluation'] })
  }
  const key = ['ai-model', id] as const
  return useMutation({
    mutationFn: async (body: AiModelUpdate) =>
      unwrap(
        await apiClient.PATCH('/api/v1/ai-models/{model_id}', {
          params: { path: { model_id: id } },
          body,
        }),
      ),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<AiModelResponse>(key)
      if (prev && typeof body.is_disabled === 'boolean') {
        qc.setQueryData<AiModelResponse>(key, { ...prev, is_disabled: body.is_disabled })
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData<AiModelResponse>(key, ctx.prev)
    },
    onSettled: () => invalidate(),
    onSuccess: () => toast.success('Model updated'),
  })
}

export function useRestoreModel() {
  // Only the registry keys: the restore is deliberately shallow (it revives the model
  // row, never the assignments or conversations the delete cascaded to), so refetching
  // the evaluation and conversation caches would be work that cannot have changed.
  const invalidate = useInvalidateModel()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.POST('/api/v1/ai-models/{model_id}/restore', {
          params: { path: { model_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('Model restored')
    },
  })
}

export function useDeleteModel() {
  const invalidate = useInvalidateModelCascade()
  const restore = useRestoreModel()
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await apiClient.DELETE('/api/v1/ai-models/{model_id}', {
          params: { path: { model_id: id } },
        }),
      ),
    onSuccess: (_data, id) => {
      invalidate()
      let undone = false
      toast.success('Model deleted', {
        // Undo brings the registry row back but not its assignments — the same shallow
        // restore the endpoint documents, so the copy must not promise more.
        description: restorableFromHint('Recently deleted on AI Models'),
        action: {
          label: 'Undo',
          // sonner leaves the button clickable while the toast animates out, so a fast
          // second click would restore an already-live row and toast its 404.
          onClick: () => {
            if (undone) return
            undone = true
            restore.mutate(id)
          },
        },
      })
    },
  })
}

export function useSetApiKey(id: string) {
  const invalidate = useInvalidateModel(id)
  return useMutation({
    mutationFn: async (api_key: string) =>
      unwrap(
        await apiClient.PUT('/api/v1/ai-models/{model_id}/api-key', {
          params: { path: { model_id: id } },
          body: { api_key },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('API key set')
    },
  })
}

export function useBulkCreateModels() {
  const invalidate = useInvalidateModel()
  return useMutation({
    mutationFn: async (body: AiModelBulkRequest) =>
      unwrap(await apiClient.POST('/api/v1/ai-models/bulk', { body })),
    onSuccess: (res) => {
      if (res.dry_run) return
      invalidate()
      notifyBulk(res, 'models created')
    },
  })
}

export function useBulkSetApiKeys() {
  const invalidate = useInvalidateModel()
  return useMutation({
    mutationFn: async (body: AiModelApiKeyBulkRequest) =>
      unwrap(await apiClient.POST('/api/v1/ai-models/api-keys/bulk', { body })),
    onSuccess: (res) => {
      if (res.dry_run) return
      invalidate()
      notifyBulk(res, 'API keys set')
    },
  })
}

// The check is async: the endpoint flips the row to `checking` and returns it (202, or 200 if a
// check is already in flight). Seed the detail cache with that response so the UI reflects `checking`
// immediately; the `useAiModel` poll then settles it to `alive`/`dead`.
export function useRunHealthCheck(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.POST('/api/v1/ai-models/{model_id}/health-check', {
          params: { path: { model_id: id } },
        }),
      ),
    onSuccess: (model) => {
      qc.setQueryData<AiModelResponse>(['ai-model', id], model)
      qc.invalidateQueries({ queryKey: ['ai-models'] })
      toast.success('Health check started')
    },
  })
}

export function useClearApiKey(id: string) {
  const invalidate = useInvalidateModel(id)
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await apiClient.DELETE('/api/v1/ai-models/{model_id}/api-key', {
          params: { path: { model_id: id } },
        }),
      ),
    onSuccess: () => {
      invalidate()
      toast.success('API key cleared')
    },
  })
}
