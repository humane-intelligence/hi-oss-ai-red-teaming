import { useEffect, useState } from 'react'
import { Controller, useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { applyApiError } from '@/lib/api/form'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useAiModels } from '@/features/ai-models/queries'
import { modelLabelSummary } from '@/features/ai-models/labels'
import {
  MANAGED_PARAM_KEYS,
  MANAGED_PARAM_NAMES,
  buildParameters,
  paramsSchemaShape,
  paramsToForm,
} from '@/features/conversations/inference-params'
import { AdvancedParams } from '@/features/conversations/advanced-params'
import { useAssignment } from './queries'
import { useAssignModel, useUpdateAssignment } from './mutations'
import type { InferenceParams } from '@/lib/api/types'
import { FormField } from '@/components/shared/form-field'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'

const MODEL_ERROR_ID = 'assign-model-error'

const schema = z.object({
  model_id: z.string().min(1, 'Select a model'),
  model_display_mask: z.string(),
  ...paramsSchemaShape,
})
type FormValues = z.infer<typeof schema>

const EMPTY: FormValues = {
  model_id: '',
  model_display_mask: '',
  system_prompt: '',
  temperature: '',
  top_p: '',
  top_k: '',
  max_tokens: '',
  frequency_penalty: '',
  presence_penalty: '',
  repetition_penalty: '',
  seed: '',
}

export function AssignModelDialog({
  evaluationId,
  open,
  onOpenChange,
  assignmentId,
  modelName,
  inherited,
  advancedParamsDisabled,
}: {
  evaluationId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  // When set, the dialog edits that assignment's params/mask instead of creating one.
  assignmentId?: string
  modelName?: string | null
  // Effective params inherited from the model config, shown under each field. In
  // edit mode the parent supplies the assignment's effective params; in create
  // mode they're derived from the picked catalog model below.
  inherited?: Record<string, unknown>
  // From the parent's assignment row, so the panel can be hidden even when the
  // caller cannot read the catalog to resolve the model itself.
  advancedParamsDisabled?: boolean
}) {
  const isEdit = Boolean(assignmentId)
  const { has } = usePermissions()
  // The catalog (GET /ai-models) needs models:read; assignment edit needs only
  // evaluations:update, so an Owner can reach this dialog without catalog access.
  // Gate the lookup on the permission (and on `open`) so it never 403s for them.
  const canReadModels = has('models:read')
  // Assigning (create): offer only models assignable to this evaluation — its group's
  // subset minus already-assigned. The backend authorizes an in-group owner via the
  // evaluation's group, so the picker works without the global `models:read`, and
  // only assigners (who can open this dialog) reach it. Editing an existing
  // assignment: fetch the full catalog (gated on `models:read`) to resolve the
  // already-assigned model's name/params — `assignable_to_evaluation` would exclude it.
  const models = useAiModels(
    isEdit
      ? { limit: 100, offset: 0 }
      : { limit: 100, offset: 0, assignable_to_evaluation: evaluationId },
    { enabled: open && (isEdit ? canReadModels : true) },
  )
  const assign = useAssignModel(evaluationId)
  const update = useUpdateAssignment(evaluationId)
  const assignment = useAssignment(evaluationId, open && assignmentId ? assignmentId : '')
  // null = follow the default (auto-expand when editing a row that already has
  // overrides); a boolean is the user's explicit toggle.
  const [paramsOpen, setParamsOpen] = useState<boolean | null>(null)
  const { register, handleSubmit, reset, setError, formState, control, setValue } =
    useForm<FormValues>({
      resolver: zodResolver(schema),
      defaultValues: EMPTY,
    })

  const storedParamsPresent =
    isEdit && assignment.data
      ? Object.keys(paramsToForm(assignment.data.parameters)).length > 0
      : false
  const showParams = paramsOpen ?? storedParamsPresent

  // Edit mode replaces the whole layer, so clearing every managed field wipes the
  // overrides set here on save (unmanaged keys are still preserved). Warn when
  // that's the pending outcome. useWatch is scoped to the managed fields so
  // typing in model_id / model_display_mask doesn't re-render them.
  const managedValues = useWatch({ control, name: MANAGED_PARAM_NAMES })
  const clearHint =
    isEdit && managedValues.every((v) => !String(v ?? '').trim())
      ? 'Saving with these blank clears all parameter overrides set here.'
      : undefined

  const selectedModelId = useWatch({ control, name: 'model_id' })

  // The edit endpoint returns the real (unmasked) model_id. When the caller can
  // read the catalog, resolve it to a name so they see which model they're
  // editing even on a masked evaluation (where the read view returns name=null).
  // Without models:read we can't resolve it, so fall back to the masked label.
  const editModelId = assignment.data?.model_id
  const resolvedModel =
    canReadModels && editModelId ? models.data?.items.find((m) => m.id === editModelId) : undefined
  const modelResolving = canReadModels && (assignment.isLoading || models.isLoading)
  const modelLabel = resolvedModel
    ? `${resolvedModel.name} (${resolvedModel.model_alias})`
    : modelResolving
      ? 'Loading…'
      : (modelName ?? '— masked —')

  // What a blank field falls back to. Edit mode → the model baseline
  // (what clearing an override actually falls back to; the parent's `inherited`
  // snapshot is stale once an override is cleared), falling back to it when the
  // catalog isn't readable. Create mode → the picked catalog model's baseline.
  const inheritedParams = isEdit
    ? ((resolvedModel?.parameters as Record<string, unknown> | undefined) ?? inherited)
    : (models.data?.items.find((m) => m.id === selectedModelId)?.parameters as
        Record<string, unknown> | undefined)

  // Same resolution order as the inherited values: the catalog row is authoritative
  // (it tracks a toggle made since the page loaded), the parent's snapshot covers a
  // caller who cannot read the catalog.
  //
  // Unknown resolves to "not disabled" — deliberately open, not an oversight. Failing
  // closed would hide the panel from an in-group owner without `models:read`, who is a
  // legitimate editor of this layer, to guard a case the gateway already covers: a
  // suppressed model drops the override at `_build_call` whatever this decides. The only
  // window where both sources are unknown is the catalog's first load, and it corrects
  // itself when the query resolves.
  const paramsDisabled = isEdit
    ? (resolvedModel?.advanced_params_disabled ?? advancedParamsDisabled ?? false)
    : (models.data?.items.find((m) => m.id === selectedModelId)?.advanced_params_disabled ?? false)

  // Edit mode: prefill once the assignment (with its parameters) loads.
  useEffect(() => {
    if (!open || !isEdit || !assignment.data) return
    reset({
      ...EMPTY,
      model_id: assignment.data.model_id,
      model_display_mask: assignment.data.model_display_mask ?? '',
      ...paramsToForm(assignment.data.parameters),
    })
  }, [open, isEdit, assignment.data, reset])

  const onSubmit = handleSubmit(async (values) => {
    // The panel is unmounted for a params-disabled model, but RHF keeps an unmounted
    // field's value — so values typed before the picker moved to such a model would
    // otherwise be written as an override the gateway never applies.
    const overrides = paramsDisabled ? undefined : buildParameters(values)
    try {
      if (isEdit && assignmentId) {
        // PATCH replaces the whole layer, so re-send the full set: the managed
        // knobs from the form, merged over any stored keys the form doesn't
        // manage (so editing temperature can't silently drop e.g. stop_sequences).
        // Clearing every managed knob then sends just the preserved keys, or {}.
        const preserved = Object.fromEntries(
          Object.entries(assignment.data?.parameters ?? {}).filter(
            ([key]) => !MANAGED_PARAM_KEYS.has(key),
          ),
        ) as InferenceParams
        await update.mutateAsync({
          assignmentId,
          body: {
            model_display_mask: values.model_display_mask || null,
            // Omitted, not emptied, for a disabled model: the layer is left exactly as
            // stored so re-enabling the flag restores the overrides, as the model form
            // promises. An omitted `parameters` means "leave unchanged" server-side.
            ...(paramsDisabled ? {} : { parameters: { ...preserved, ...overrides } }),
          },
        })
      } else {
        await assign.mutateAsync({
          model_id: values.model_id,
          model_display_mask: values.model_display_mask || null,
          ...(overrides ? { parameters: overrides } : {}),
        })
      }
      onOpenChange(false)
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? 'Edit model parameters' : 'Assign model'}
      onOpen={() => {
        setParamsOpen(null)
        if (!isEdit) reset(EMPTY)
      }}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {isEdit ? (
          <FormField label="Model">
            <p className="rounded-md border px-3 py-2 text-sm">{modelLabel}</p>
          </FormField>
        ) : (
          <FormField
            label="Model"
            htmlFor="assign-model"
            error={formState.errors.model_id?.message}
            errorId={MODEL_ERROR_ID}
          >
            <Controller
              name="model_id"
              control={control}
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  {/* Wired by hand: `Controller` renders through a prop, so `FormField` cannot
                      clone the trigger and its cloned aria props would go nowhere. */}
                  <SelectTrigger
                    id="assign-model"
                    aria-invalid={formState.errors.model_id ? true : undefined}
                    aria-describedby={formState.errors.model_id ? MODEL_ERROR_ID : undefined}
                  >
                    <SelectValue placeholder="— select model —" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {/* Labels are appended to the name rather than badged: the point is that a
                          fine-tuned model cannot be assigned by mistake. Summarised, with the full
                          set on `title`. */}
                      {(models.data?.items ?? []).map((m) => (
                        <SelectItem
                          key={m.id}
                          value={m.id}
                          title={m.labels.join(', ') || undefined}
                        >
                          {m.name} ({m.model_alias})
                          {m.labels.length > 0 ? ` — ${modelLabelSummary(m.labels)}` : ''}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              )}
            />
          </FormField>
        )}
        <FormField
          label="Display mask (optional)"
          htmlFor="assign-mask"
          error={formState.errors.model_display_mask?.message}
        >
          <Input
            id="assign-mask"
            placeholder="Alias shown when the evaluation masks models"
            {...register('model_display_mask')}
          />
        </FormField>
        {paramsDisabled ? (
          <p className="text-muted-foreground text-xs">
            This model ignores advanced parameters, so there is nothing to override here. Any
            overrides already saved are kept, and start applying again if the model's setting is
            turned back off.
          </p>
        ) : (
          <AdvancedParams
            idPrefix="assign"
            open={showParams}
            onToggle={() => setParamsOpen(!showParams)}
            register={register}
            control={control}
            setValue={setValue}
            errors={formState.errors}
            hint={clearHint}
            inherited={inheritedParams}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={formState.isSubmitting || (isEdit && !assignment.data)}>
            {formState.isSubmitting
              ? isEdit
                ? 'Saving…'
                : 'Assigning…'
              : isEdit
                ? 'Save'
                : 'Assign'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
