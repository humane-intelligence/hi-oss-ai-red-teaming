import type {
  Control,
  FieldErrors,
  FieldValues,
  Path,
  PathValue,
  UseFormRegister,
  UseFormSetValue,
} from 'react-hook-form'
import { useWatch } from 'react-hook-form'
import {
  MANAGED_PARAM_NAMES,
  NUMERIC_PARAMS,
  PARAMS_PANEL_HINT,
  PARAMS_PANEL_HINT_BASELINE,
  SYSTEM_PROMPT_HINT,
  inheritedValue,
} from './inference-params'
import { ParamField, SystemPromptField } from './param-field'

// String-typed fields this block edits: the override system prompt + the shared
// numeric knobs. Any form embedding <AdvancedParams> must carry these keys.
export type AdvancedParamsValues = { system_prompt: string } & Record<
  (typeof NUMERIC_PARAMS)[number][0],
  string
>

// The collapsible "Advanced model parameters" block (system prompt + NUMERIC_PARAMS),
// shared by the assign-model, conversation-group and AI-model baseline forms.
// `idPrefix` scopes the input ids per host form; the toggle state stays with the
// caller (open/onToggle) so it can drive defaults — e.g. auto-expand when stored
// overrides exist.
export function AdvancedParams<T extends FieldValues & AdvancedParamsValues>({
  idPrefix,
  open,
  onToggle,
  register,
  control,
  setValue,
  errors,
  hint,
  inherited,
  baseline,
}: {
  idPrefix: string
  open: boolean
  onToggle: () => void
  register: UseFormRegister<T>
  // `control` rather than `watch`: the clear affordance needs each field's value,
  // and useWatch keeps the re-render inside this panel instead of the host form.
  control: Control<T>
  setValue: UseFormSetValue<T>
  errors: FieldErrors<T>
  // Optional note rendered inside the open panel (e.g. the edit-mode clear warning).
  hint?: string
  // Effective params from higher in the hierarchy (model ⊕ assignment), rendered as
  // a line under each field so a blank one shows what it falls back to.
  inherited?: Record<string, unknown>
  // The cascade root (AI-model baseline): nothing upstream, so drop the
  // "inherit"/"override" wording.
  baseline?: boolean
}) {
  const fieldErrors = errors as unknown as Record<string, { message?: string } | undefined>
  const managed = useWatch({ control, name: MANAGED_PARAM_NAMES as unknown as Path<T>[] })
  // Emptiness, not truthiness: a legitimate 0 arrives as the string "0".
  const filled = (key: string) =>
    String(managed[MANAGED_PARAM_NAMES.indexOf(key as never)] ?? '') !== ''
  const clear = (key: string) =>
    setValue(key as Path<T>, '' as PathValue<T, Path<T>>, {
      shouldDirty: true,
      // Otherwise a "Must be a number" error survives under a now-empty field.
      shouldValidate: true,
    })

  return (
    <div>
      <button
        type="button"
        className="text-muted-foreground hover:text-foreground text-sm"
        aria-expanded={open}
        onClick={onToggle}
      >
        {open ? '▾' : '▸'} Advanced model parameters
      </button>
      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-muted-foreground text-xs">
            {baseline ? PARAMS_PANEL_HINT_BASELINE : PARAMS_PANEL_HINT}
          </p>
          {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
          <SystemPromptField
            id={`${idPrefix}-system-prompt`}
            hint={SYSTEM_PROMPT_HINT}
            inherited={inheritedValue(inherited, 'system_prompt')}
            filled={filled('system_prompt')}
            onClear={() => clear('system_prompt')}
            inputProps={register('system_prompt' as Path<T>)}
          />
          {NUMERIC_PARAMS.map(([key, label, step, description]) => (
            <ParamField
              key={key}
              id={`${idPrefix}-${key}`}
              label={label}
              hint={description}
              step={step}
              error={fieldErrors[key]?.message}
              inherited={inheritedValue(inherited, key)}
              filled={filled(key)}
              onClear={() => clear(key)}
              inputProps={register(key as Path<T>)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
