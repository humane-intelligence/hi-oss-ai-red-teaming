import { useEffect, useMemo, useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useAiModel, useAiModelLabels } from './queries'
import { useCreateModel, useUpdateModel } from './mutations'
import {
  CUSTOM_PROVIDERS,
  isModelKind,
  MODALITIES,
  modalityLabel,
  MODEL_KINDS,
  PROVIDERS,
  providerLabel,
  providersForKind,
  requiresInferenceEndpoint,
  VENDOR_PROVIDERS,
  type ModelKind,
} from './labels'
import { ModelKindDialog } from './model-kind-dialog'
import { AdvancedParams } from '@/features/conversations/advanced-params'
import {
  MANAGED_PARAM_KEYS,
  buildParameters,
  paramsSchemaShape,
  paramsToForm,
} from '@/features/conversations/inference-params'
import type { InferenceParams } from '@/lib/api/types'
import { applyApiError } from '@/lib/api/form'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { MultiSearchSelect } from '@/components/shared/multi-search-select'
import { InfoHint } from '@/components/shared/info-hint'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PlainSelect } from '@/components/ui/plain-select'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent } from '@/components/ui/card'

// Says who owns the value, because the reported confusion was exactly that: whether
// the field reports a capability or sets one.
const MODALITY_HINT =
  'What this model can take in and give back. You declare it — nothing detects it — and the console ' +
  'takes you at your word: it offers image attachments only where you allow them, and refuses a model ' +
  "that can't answer in text."

// Mirror the caps the contract publishes (`maxItems` / `items.maxLength` on the write schemas), so
// the form says no before the request does. The per-label length counts code points, not the UTF-16
// units `.length` yields: the API counts code points, and a stricter mirror would refuse a 64-emoji
// label the server accepts.
const MAX_LABELS = 20
const MAX_LABEL_LENGTH = 64

const editSchema = z.object({
  name: z.string().min(1, 'Required'),
  description: z.string().max(2000, 'At most 2000 characters'),
  model_alias: z.string().min(1, 'Required'),
  provider: z.enum(PROVIDERS),
  input_modalities: z.array(z.enum(MODALITIES)),
  output_modalities: z.array(z.enum(MODALITIES)).min(1, 'Select at least one output modality'),
  provider_model_id: z.string().min(1, 'Required'),
  endpoint_name: z.string(),
  inference_endpoint: z.string(),
  icon_file: z.string(),
  // Both caps fail the array, not an item: a per-item issue keys the error at `labels.0`, where the
  // field's `errors.labels?.message` reads undefined — no message, and no submit either.
  labels: z
    .array(z.string())
    .max(MAX_LABELS, `At most ${MAX_LABELS} labels`)
    .refine(
      (ls) => ls.every((l) => [...l].length <= MAX_LABEL_LENGTH),
      `At most ${MAX_LABEL_LENGTH} characters per label`,
    ),
  is_disabled: z.boolean(),
  warmup_enabled: z.boolean(),
  advanced_params_disabled: z.boolean(),
  inactivity_alert_hours: z
    .string()
    .refine(
      (v) => v.trim() === '' || (/^\d+$/.test(v.trim()) && Number(v) >= 1 && Number(v) <= 8760),
      'Must be a whole number of hours between 1 and 8760',
    ),
  api_key: z.string(),
  ...paramsSchemaShape,
})

// Create validates the endpoint locally: required for `generic`, and an absolute http(s)
// URL whenever it is filled. Edit defers both to the backend — the patch carries the field
// only when it moved, so a row predating either rule stays renameable, and a genuinely bad
// edit comes back as a field-addressable 422 that `applyApiError` puts on the input.
const createSchema = editSchema
  .refine((v) => !requiresInferenceEndpoint(v.provider) || v.inference_endpoint.trim().length > 0, {
    path: ['inference_endpoint'],
    message: 'Required — a generic provider has no vendor-hosted URL to fall back on',
  })
  .refine(
    (v) => v.inference_endpoint.trim() === '' || /^https?:\/\//i.test(v.inference_endpoint.trim()),
    {
      path: ['inference_endpoint'],
      message: 'Must start with http:// or https://',
    },
  )
type FormValues = z.infer<typeof editSchema>

const EMPTY: FormValues = {
  name: '',
  description: '',
  model_alias: '',
  provider: 'openai',
  input_modalities: [],
  output_modalities: ['text'],
  provider_model_id: '',
  endpoint_name: '',
  inference_endpoint: '',
  icon_file: '',
  labels: [],
  is_disabled: false,
  warmup_enabled: false,
  advanced_params_disabled: false,
  inactivity_alert_hours: '',
  api_key: '',
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

export function AiModelFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const existing = useAiModel(id ?? '')
  const create = useCreateModel()
  const update = useUpdateModel(id ?? '')

  // Create is entered through the kind dialog, which puts the choice in the URL. It
  // is fixed for the life of the form — picking again means going back to the dialog,
  // so nothing here can silently rewrite `provider` out from under the user.
  const [searchParams] = useSearchParams()
  const kindParam = searchParams.get('kind')
  // Create-only, and ignored outright on edit: a hand-typed `?kind=` on an edit URL
  // would otherwise narrow the provider list past the row's own value, and the select
  // would silently fall back to the first option it does offer.
  const kind = !isEdit && isModelKind(kindParam) ? kindParam : null

  const { register, handleSubmit, reset, setError, setValue, control, clearErrors, formState } =
    useForm<FormValues>({
      resolver: zodResolver(isEdit ? editSchema : createSchema),
      defaultValues: kind === 'custom' ? { ...EMPTY, provider: CUSTOM_PROVIDERS[0] } : EMPTY,
    })

  const [labelSearch, setLabelSearch] = useState('')
  const labelVocabulary = useAiModelLabels()
  const labels = useWatch({ control, name: 'labels' })
  // The labels endpoint has no search parameter — a picker needs the whole vocabulary — so the
  // search box narrows it here, like the group form narrows the model registry.
  const labelOptions = useMemo(() => {
    const term = labelSearch.trim().toLowerCase()
    return (labelVocabulary.data ?? [])
      .filter((l) => l.toLowerCase().includes(term))
      .map((l) => ({ value: l, label: l }))
  }, [labelVocabulary.data, labelSearch])

  const provider = useWatch({ control, name: 'provider' })
  const warmupEnabled = useWatch({ control, name: 'warmup_enabled' })
  const advancedParamsDisabled = useWatch({ control, name: 'advanced_params_disabled' })
  // On edit the row's own provider decides; the URL kind belongs to the create flow.
  const showEndpointFields = isEdit || kind === 'custom'

  // Whether the URL is required rides on `provider`, so a stale "Required" would
  // otherwise sit under an optional field until the next submit.
  const onProviderChange = () => clearErrors('inference_endpoint')

  // Re-picking the kind reopens the same chooser over the form rather than sending the
  // user back to the list. Safe to reset the provider — and drop a typed URL, which the
  // other half has no field for — where the tabs were not: this is create-only (nothing
  // persisted to lose) and an explicit choice, not a view toggle you pass back through.
  const [kindDialogOpen, setKindDialogOpen] = useState(false)
  const onChangeKind = (next: ModelKind) => {
    setKindDialogOpen(false)
    if (next === kind) return
    navigate(`/ai-models/new?kind=${next}`, { replace: true })
    setValue('provider', next === 'custom' ? CUSTOM_PROVIDERS[0] : VENDOR_PROVIDERS[0], {
      shouldDirty: true,
    })
    setValue('inference_endpoint', '')
    clearErrors('inference_endpoint')
  }

  // null = follow the default (auto-expand when the model already has params);
  // a boolean is the user's explicit toggle.
  const [paramsOpen, setParamsOpen] = useState<boolean | null>(null)
  const showParams =
    paramsOpen ?? (isEdit && Object.keys(paramsToForm(existing.data?.parameters)).length > 0)

  useEffect(() => {
    const m = existing.data
    if (!m) return
    // Spread EMPTY first: `paramsToForm` omits knobs the model doesn't set, and a
    // missing numeric field would reset to `undefined` — which fails the `z.string()`
    // param schema in the collapsed Advanced section, silently blocking Save.
    reset({
      ...EMPTY,
      name: m.name,
      description: m.description ?? '',
      model_alias: m.model_alias,
      provider: m.provider,
      input_modalities: m.input_modalities.filter((mod) => mod !== 'text'),
      output_modalities: m.output_modalities,
      provider_model_id: m.provider_model_id,
      endpoint_name: m.endpoint_name ?? '',
      inference_endpoint: m.inference_endpoint ?? '',
      icon_file: m.icon_file ?? '',
      labels: m.labels,
      is_disabled: m.is_disabled,
      warmup_enabled: m.warmup_enabled,
      advanced_params_disabled: m.advanced_params_disabled,
      inactivity_alert_hours: m.inactivity_alert_hours?.toString() ?? '',
      api_key: '',
      ...paramsToForm(m.parameters),
    })
  }, [existing.data, reset])

  useUnsavedGuard(formState.isDirty)

  const onSubmit = handleSubmit(async (v) => {
    // Unguarded on purpose, unlike the assignment and conversation panels, which drop what
    // was typed once their model turns out to ignore parameters. This form edits the row the
    // knobs belong to, so persisting them is what makes the toggle reversible — the values
    // are inert while it is on and reappear when it is off. A layer below would instead be
    // storing an override about a model that ignores overrides.
    const overrides = buildParameters(v)
    const base = {
      name: v.name,
      // Blank input clears the column rather than storing an empty string.
      description: v.description.trim() || null,
      model_alias: v.model_alias,
      provider: v.provider,
      // The field tracks only what is actually choosable; `text` is mandatory and its box
      // is disabled, so it is added here rather than carried in the value — otherwise
      // toggling Image on and off would leave the form permanently dirty.
      input_modalities: ['text' as const, ...v.input_modalities],
      output_modalities: v.output_modalities,
      provider_model_id: v.provider_model_id,
      // Carried as-is even though the form may not show them. A vendor row may
      // legitimately point at a proxy (dispatch feeds `api_base` for every vendor);
      // `endpoint_name` and `icon_file` have no input at all any more — nothing reads
      // them, so they are no longer offered, but rows that carry a value keep it
      // instead of losing it to an unrelated edit. (The edit path below drops
      // `inference_endpoint` from the patch when it has not moved.)
      endpoint_name: v.endpoint_name || null,
      inference_endpoint: v.inference_endpoint || null,
      icon_file: v.icon_file || null,
      labels: v.labels,
      is_disabled: v.is_disabled,
      warmup_enabled: v.warmup_enabled,
      advanced_params_disabled: v.advanced_params_disabled,
      // Blank clears the threshold; unchecking Warm up must not, or an unrelated edit
      // would wipe a stored value the form no longer shows (the sweep already ignores
      // non-warmup models, so the coupling bought nothing).
      inactivity_alert_hours: v.inactivity_alert_hours.trim()
        ? Number(v.inactivity_alert_hours)
        : null,
    }
    try {
      if (isEdit) {
        // PATCH replaces the whole parameters layer, so re-send the managed knobs
        // merged over any stored keys the editor doesn't manage.
        const preserved = Object.fromEntries(
          Object.entries(existing.data?.parameters ?? {}).filter(
            ([key]) => !MANAGED_PARAM_KEYS.has(key),
          ),
        ) as InferenceParams
        // The backend's generic⇒URL rule only fires when a patch touches this pair, so
        // send it only when it actually moved. Sending it every time would trip the rule
        // on every edit from here, leaving a row that predates it — `generic`, no URL —
        // impossible to rename or even disable.
        const stored = existing.data
        const { provider, inference_endpoint, ...untouched } = base
        await update.mutateAsync({
          ...untouched,
          ...(provider !== stored?.provider ? { provider } : {}),
          ...(inference_endpoint !== (stored?.inference_endpoint ?? null)
            ? { inference_endpoint }
            : {}),
          parameters: { ...preserved, ...overrides },
        })
        navigate(`/ai-models/${id}`)
      } else {
        const body = { ...base, ...(overrides ? { parameters: overrides } : {}) }
        const created = await create.mutateAsync(v.api_key ? { ...body, api_key: v.api_key } : body)
        navigate(`/ai-models/${created.id}`)
      }
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  // A create without a kind never came through the dialog — hand it back there rather
  // than guessing, so the choice is always made once and made explicitly.
  if (!isEdit && !kind) return <Navigate to="/ai-models?new" replace />

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => navigate(isEdit ? `/ai-models/${id}` : '/ai-models')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>
      <ModelKindDialog
        open={kindDialogOpen}
        onOpenChange={setKindDialogOpen}
        onChoose={onChangeKind}
      />
      <PageHeader
        title={isEdit ? 'Edit model' : 'New model'}
        breadcrumbs={
          <Breadcrumbs
            items={[{ label: 'AI Models', to: '/ai-models' }, { label: isEdit ? 'Edit' : 'New' }]}
          />
        }
      />
      <Card>
        <CardContent className="space-y-6 p-6">
          {kind && (
            // Heads the same card as the form it describes — the choice and what it
            // asks of you are one thing, not two stacked surfaces.
            <>
              <header className="flex items-start justify-between gap-4">
                <div className="space-y-1.5">
                  <h2 className="font-display text-base font-semibold tracking-tight">
                    {MODEL_KINDS[kind].title}
                  </h2>
                  <p className="text-muted-foreground text-sm leading-relaxed">
                    {MODEL_KINDS[kind].description}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  aria-label="Change model type"
                  onClick={() => setKindDialogOpen(true)}
                >
                  Change
                </Button>
              </header>
              <hr className="border-border" />
            </>
          )}
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <FormField label="Name" htmlFor="name" error={formState.errors.name?.message}>
              <Input id="name" {...register('name')} />
            </FormField>
            <FormField
              label="Description"
              htmlFor="description"
              error={formState.errors.description?.message}
            >
              <Textarea id="description" rows={3} {...register('description')} />
            </FormField>
            <div className="grid grid-cols-2 gap-4">
              {/* Provider + its model id are the pair that names the model at the vendor,
                  so they share a row. */}
              <FormField
                label="Provider"
                htmlFor="provider"
                error={formState.errors.provider?.message}
              >
                <PlainSelect
                  id="provider"
                  {...register('provider', { onChange: onProviderChange })}
                >
                  {(kind ? providersForKind(kind) : PROVIDERS).map((p) => (
                    <option key={p} value={p}>
                      {providerLabel(p)}
                    </option>
                  ))}
                </PlainSelect>
              </FormField>
              <FormField
                label="Provider model id"
                htmlFor="provider_model_id"
                error={formState.errors.provider_model_id?.message}
              >
                <Input id="provider_model_id" {...register('provider_model_id')} />
              </FormField>
              <FormField
                label="Alias"
                htmlFor="model_alias"
                error={formState.errors.model_alias?.message}
              >
                <Input id="model_alias" {...register('model_alias')} />
              </FormField>
              {showEndpointFields && (
                <FormField
                  label={
                    requiresInferenceEndpoint(provider)
                      ? 'Inference endpoint'
                      : 'Inference endpoint (optional)'
                  }
                  htmlFor="inference_endpoint"
                  error={formState.errors.inference_endpoint?.message}
                  hint="Base URL requests are sent to, e.g. https://my-endpoint.example.com/v1."
                >
                  <Input id="inference_endpoint" {...register('inference_endpoint')} />
                </FormField>
              )}
              {!isEdit && (
                <FormField label="API key (optional)" htmlFor="api_key">
                  <Input id="api_key" type="password" {...register('api_key')} />
                </FormField>
              )}
            </div>
            <fieldset className="space-y-2" aria-describedby="modality-hint">
              {/* Anything readable inside <legend> joins the fieldset's accessible NAME, so
                  the 240-char explanation must not land there. An icon-only button is safe
                  — an embedded control contributes nothing to name-from-content — and the
                  sr-only paragraph below is what `aria-describedby` announces on entry. */}
              <legend className="flex items-center gap-1.5 pb-2 text-sm font-medium">
                Model modality
                <InfoHint text={MODALITY_HINT} />
              </legend>
              <p id="modality-hint" className="sr-only">
                {MODALITY_HINT}
              </p>
              <div
                role="group"
                aria-labelledby="modality-input"
                aria-describedby="modality-hint modality-error"
                className="flex items-center gap-4 text-sm"
              >
                <span id="modality-input" className="text-muted-foreground w-16 shrink-0">
                  Input
                </span>
                <label className="text-muted-foreground flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="size-4 rounded border"
                    aria-label="Text input"
                    defaultChecked
                    disabled
                  />
                  Text
                </label>
                {MODALITIES.filter((m) => m !== 'text').map((m) => (
                  <label key={m} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      className="size-4 rounded border"
                      aria-label={`${modalityLabel(m)} input`}
                      value={m}
                      {...register('input_modalities')}
                    />
                    {modalityLabel(m)}
                  </label>
                ))}
              </div>
              <div
                role="group"
                aria-labelledby="modality-output"
                aria-describedby="modality-error"
                className="flex items-center gap-4 text-sm"
              >
                <span id="modality-output" className="text-muted-foreground w-16 shrink-0">
                  Output
                </span>
                {MODALITIES.map((m) => (
                  <label key={m} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      className="size-4 rounded border"
                      aria-label={`${modalityLabel(m)} output`}
                      value={m}
                      {...register('output_modalities')}
                    />
                    {modalityLabel(m)}
                  </label>
                ))}
              </div>
              <p className="text-muted-foreground text-xs">
                Every call carries a text prompt, so text input can't be turned off.
              </p>
              {/* One slot for the whole fieldset: a 422 aimed at either list carries
                  `errors[]`, which `query.ts` excludes from the global toast — without a
                  surface here, Save would just look dead. `role="alert"` because the only
                  error this fieldset raises on its own (unchecking every output) leaves
                  focus on the checkbox that caused it: RHF has nowhere to move focus, so
                  no `aria-describedby` is re-read and the message would reach nobody. */}
              {/* Not `empty:hidden`: a live region that enters the a11y tree in the same
                  tick as its text is ignored by some SR/browser pairs, and an empty <p>
                  with no padding costs nothing visually. */}
              <p id="modality-error" role="alert" className="text-destructive text-xs">
                {formState.errors.output_modalities?.message ??
                  formState.errors.input_modalities?.message ??
                  ''}
              </p>
            </fieldset>
            <div className="space-y-1">
              <MultiSearchSelect
                label="Labels (optional)"
                htmlFor="labels"
                value={labels}
                // `shouldValidate`: without it the cap fires only on Create, so the operator adds a
                // 21st chip and learns about it a click later.
                onChange={(next) =>
                  setValue('labels', next, { shouldDirty: true, shouldValidate: true })
                }
                onSearchChange={setLabelSearch}
                options={labelOptions}
                isPending={labelVocabulary.isPending}
                isError={labelVocabulary.isError}
                error={formState.errors.labels?.message}
                placeholder="Search or type a new label…"
                emptyLabel="No labels in use yet — type one to add it."
                allowCreate
                describedBy="labels-hint"
              />
              {/* States both caps, because otherwise the only way to learn either is to trip it.
                  Not a `maxLength` on the input: that input is the search box too, and a length
                  limit there truncates a pasted label in silence. */}
              <p id="labels-hint" className="text-muted-foreground text-xs">
                Notes for admins and owners, shown as badges on the registry and beside the model
                wherever one is picked — e.g. &ldquo;self-hosted&rdquo; or &ldquo;fine-tuning
                needed&rdquo;. Suggestions are the labels other models already carry. Up to{' '}
                {MAX_LABELS} labels, {MAX_LABEL_LENGTH} characters each.
              </p>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border"
                {...register('is_disabled')}
              />
              Disabled (won't be used for new conversations)
            </label>
            <div className="flex items-center gap-2 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="size-4 rounded border"
                  {...register('warmup_enabled')}
                />
                Warm up before chatting
              </label>
              <InfoHint text="Some endpoints scale to zero when idle and reject the first request while a replica boots. When on, the console probes the model as a conversation opens — waking it — and shows a readiness badge, so the first message doesn't fail on a cold start. Leave off for always-on models (e.g. OpenAI/Anthropic)." />
            </div>
            <div className="flex items-center gap-2 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="size-4 rounded border"
                  {...register('advanced_params_disabled')}
                />
                Disable advanced parameters
              </label>
              <InfoHint text="Turn on for a model that ignores sampling settings, or when you want every conversation to use the provider's own defaults. The parameter panels disappear here and everywhere below (evaluation assignments, conversations), and nothing is sent to the provider. Values already saved are kept, so turning this back off restores them." />
            </div>
            {/* Disabled rather than unmounted while warmup is off: RHF keeps an unmounted
                field's value, so a hidden invalid one blocked Save with no error surface. */}
            <FormField
              label="Idle alert after (hours)"
              htmlFor="inactivity_alert_hours"
              error={formState.errors.inactivity_alert_hours?.message}
              hint="Email and notify the admins when nobody has messaged this model for this long, so an endpoint left warm doesn't keep costing money unnoticed. Opening a conversation warms the model but doesn't count as use — that's the case worth catching. Only warmed models are checked; leave blank for no alert."
            >
              <Input
                id="inactivity_alert_hours"
                inputMode="numeric"
                placeholder="e.g. 48"
                disabled={!warmupEnabled}
                {...register('inactivity_alert_hours')}
              />
              {/* The popover hint is opt-in discovery; name the unblocking toggle right
                  where the greyed field is. */}
              {!warmupEnabled && (
                <p className="text-muted-foreground text-xs">
                  Turn on “Warm up before chatting” to set this.
                </p>
              )}
            </FormField>
            {/* Hidden, not disabled: stored values survive the toggle, so re-enabling brings
                them back rather than leaving the operator with silently-inert inputs. */}
            {!advancedParamsDisabled && (
              <AdvancedParams
                idPrefix="model"
                open={showParams}
                onToggle={() => setParamsOpen(!showParams)}
                register={register}
                control={control}
                setValue={setValue}
                errors={formState.errors}
                baseline
              />
            )}
            <div className="flex justify-end gap-2 pt-2">
              {/* Same destination as Back. `navigate(-1)` would land on `/ai-models?new`
                  and reopen the chooser the user just came through. */}
              <Button
                type="button"
                variant="outline"
                onClick={() => navigate(isEdit ? `/ai-models/${id}` : '/ai-models')}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={formState.isSubmitting || (isEdit && !existing.data)}>
                {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Create'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
