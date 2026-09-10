import { useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Loader2, Plus, X } from 'lucide-react'
import { useEvaluation } from '@/features/evaluations/queries'
import { useEvaluationScenarios } from '@/features/scenarios/queries'
import { useGroupAuthority } from './use-group-authority'
import { AuthorityError } from './authority-error'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { useCreateConversationGroup } from './mutations'
import { ParamField, SystemPromptField } from './param-field'
import {
  NUMERIC_PARAMS,
  PARAMS_PANEL_HINT,
  SYSTEM_PROMPT_HINT,
  buildParameters,
  inheritedValue,
} from './inference-params'
import { FormField } from '@/components/shared/form-field'
import { ApiError, fieldErrorsFromProblem } from '@/lib/api/problem'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Label } from '@/components/ui/label'

type Row = {
  key: string
  modelId: string
  title: string
  showParams: boolean
  params: Record<string, string>
}

let rowSeq = 0
function emptyRow(modelId: string): Row {
  rowSeq += 1
  return { key: `row-${rowSeq}`, modelId, title: '', showParams: false, params: {} }
}

export function GroupCreatePage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const evalId = id ?? ''

  // The route gate is coarse, so the real check happens here — against the parent group's
  // `user_permissions` as well as the JWT, the same either/or the server applies.
  const authority = useGroupAuthority(evalId, 'conversations:create')
  const evaluation = useEvaluation(evalId)
  const scenarios = useEvaluationScenarios(evalId)
  const createGroup = useCreateConversationGroup()

  const models = evaluation.data?.models ?? []
  const modelName = (assignmentId: string) =>
    models.find((m) => m.assignment_id === assignmentId)?.name ?? '— masked —'

  const [searchParams] = useSearchParams()
  const [name, setName] = useState('')
  const [scenarioId, setScenarioId] = useState(() => searchParams.get('scenario') ?? '')
  // Membership check, not just non-empty: a stale `?scenario=` deep-link (deleted scenario,
  // old URL) must neither arm Start nor leave the native select displaying the first live
  // option it never chose — render the placeholder instead. Derived, not reset in an effect.
  const scenarioChosen = (scenarios.data?.items ?? []).some((s) => s.id === scenarioId)
  // One value for both the select and the payload, so the two can't drift apart.
  const chosenScenarioId = scenarioChosen ? scenarioId : ''
  // One row by default; "Add model" appends more to run them side by side.
  const [rows, setRows] = useState<Row[]>(() => [emptyRow('')])

  // Each row defaults to a distinct evaluation model until the user overrides it;
  // derived (not stored) so it tracks the async-loaded model list without a setState-in-render.
  const effModel = (row: Row, i: number) =>
    row.modelId || (models.length ? models[i % models.length]!.assignment_id : '')

  // Effective params (model ⊕ assignment) the conversation overrides layer on top of.
  const inheritedParams = (row: Row, i: number) =>
    models.find((m) => m.assignment_id === effModel(row, i))?.effective_parameters

  const paramsDisabled = (row: Row, i: number) =>
    models.find((m) => m.assignment_id === effModel(row, i))?.advanced_params_disabled ?? false

  const setRow = (key: string, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)))
  const setRowParam = (key: string, field: string, value: string) =>
    setRows((rs) =>
      rs.map((r) => (r.key === key ? { ...r, params: { ...r.params, [field]: value } } : r)),
    )

  // Surface only field errors (422) inline — anything else double-notifies with the
  // global toast. A per-row title 422 collides on one `title` key (unreachable: trimmed + capped).
  const titleError =
    createGroup.error instanceof ApiError
      ? fieldErrorsFromProblem(createGroup.error.problem).title
      : undefined

  const canStart =
    models.length > 0 &&
    scenarioChosen &&
    rows.length > 0 &&
    rows.every((r, i) => effModel(r, i) !== '') &&
    !createGroup.isPending

  const handleStart = () => {
    if (!canStart) return
    const payloadModels = rows.map((r, i) => {
      // Values typed before the row was pointed at a params-disabled model would
      // otherwise ride along, storing an override the gateway will never apply.
      const parameters = paramsDisabled(r, i) ? undefined : buildParameters(r.params)
      const title = r.title.trim()
      return {
        evaluation_ai_model_id: effModel(r, i),
        ...(title ? { title } : {}),
        ...(parameters ? { parameters } : {}),
      }
    })
    const fallbackName = `Compare: ${rows.map((r, i) => modelName(effModel(r, i))).join(' vs ')}`
    createGroup.mutate(
      {
        scenarioId: chosenScenarioId,
        body: { name: name.trim() || fallbackName, models: payloadModels },
      },
      { onSuccess: (group) => navigate(`/evaluations/${evalId}/conversation-groups/${group.id}`) },
    )
  }

  if (authority.pending) return <DetailSkeleton />
  if (authority.failed)
    return <AuthorityError error={authority.error} backTo={`/evaluations/${evalId}`} />
  if (!authority.granted) return <NotAuthorized />

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2"
          onClick={() => navigate(`/evaluations/${evalId}`)}
        >
          <ArrowLeft className="size-4" /> Back to evaluation
        </Button>
        <h1 className="text-xl font-semibold">New conversation</h1>
      </div>

      {evaluation.isPending && <p className="text-muted-foreground">Loading…</p>}

      {evaluation.data && (
        <div className="bg-card space-y-5 rounded-lg border p-4">
          <p className="text-muted-foreground text-sm">
            Start a conversation with a model. Add more models to run them side by side — or repeat
            a model to compare it at different parameters.
          </p>

          <FormField label="Name (optional)" htmlFor="group-name">
            <Input
              id="group-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Auto-named from the models if left blank"
            />
          </FormField>

          <FormField label="Scenario" htmlFor="group-scenario">
            <Select value={chosenScenarioId} onValueChange={setScenarioId}>
              <SelectTrigger id="group-scenario">
                <SelectValue placeholder="Select a scenario…" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(scenarios.data?.items ?? []).map((s) => (
                    <SelectItem key={s.id} value={s.id}>
                      {s.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            {scenarios.data && scenarios.data.items.length === 0 && (
              <p className="text-muted-foreground text-sm">
                Every conversation targets a scenario — add one on the evaluation page first.
              </p>
            )}
          </FormField>

          <div className="space-y-3">
            <Label>Models</Label>
            {rows.map((row, i) => (
              <div key={row.key} className="rounded-md border p-3">
                {/* Stacked below `sm`: the Advanced-parameters button's label cannot shrink. */}
                <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                  <div className="min-w-0 flex-1">
                    <FormField label={`Model ${i + 1}`} htmlFor={`group-model-${i}`}>
                      <Select
                        value={effModel(row, i)}
                        onValueChange={(v) => setRow(row.key, { modelId: v })}
                      >
                        <SelectTrigger id={`group-model-${i}`}>
                          <SelectValue placeholder="— select model —" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {models.map((m) => (
                              <SelectItem key={m.assignment_id} value={m.assignment_id}>
                                {m.name ?? '— masked —'}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                    </FormField>
                  </div>
                  <div className="min-w-0 flex-1">
                    <FormField label={`Title ${i + 1} (optional)`} htmlFor={`group-title-${i}`}>
                      <Input
                        id={`group-title-${i}`}
                        value={row.title}
                        onChange={(e) => setRow(row.key, { title: e.target.value })}
                        placeholder="e.g. Direct ask"
                        maxLength={255}
                      />
                    </FormField>
                  </div>
                  {!paramsDisabled(row, i) && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-expanded={row.showParams}
                      onClick={() => setRow(row.key, { showParams: !row.showParams })}
                    >
                      {row.showParams ? '▾' : '▸'} Advanced model parameters
                    </Button>
                  )}
                  {rows.length > 1 && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove model ${i + 1}`}
                      onClick={() => setRows((rs) => rs.filter((r) => r.key !== row.key))}
                    >
                      <X className="size-4" />
                    </Button>
                  )}
                </div>

                {row.showParams && !paramsDisabled(row, i) && (
                  <div className="mt-3 space-y-3 border-t pt-3">
                    <p className="text-muted-foreground text-xs">{PARAMS_PANEL_HINT}</p>
                    <SystemPromptField
                      id={`group-system-${i}`}
                      hint={SYSTEM_PROMPT_HINT}
                      inherited={inheritedValue(inheritedParams(row, i), 'system_prompt')}
                      filled={(row.params.system_prompt ?? '') !== ''}
                      onClear={() => setRowParam(row.key, 'system_prompt', '')}
                      inputProps={{
                        value: row.params.system_prompt ?? '',
                        onChange: (e) => setRowParam(row.key, 'system_prompt', e.target.value),
                      }}
                    />
                    <div className="grid grid-cols-[minmax(0,1fr)] gap-3 sm:grid-cols-2">
                      {NUMERIC_PARAMS.map(([key, label, step, description]) => (
                        <ParamField
                          key={key}
                          id={`group-${key}-${i}`}
                          label={label}
                          hint={description}
                          step={step}
                          inherited={inheritedValue(inheritedParams(row, i), key)}
                          filled={(row.params[key] ?? '') !== ''}
                          onClear={() => setRowParam(row.key, key, '')}
                          inputProps={{
                            value: row.params[key] ?? '',
                            onChange: (e) => setRowParam(row.key, key, e.target.value),
                          }}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}

            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={models.length === 0}
              onClick={() => setRows((rs) => [...rs, emptyRow(models[0]?.assignment_id ?? '')])}
            >
              <Plus className="size-4" /> Add model
            </Button>
          </div>

          {models.length === 0 && (
            <p className="text-muted-foreground text-sm">
              Assign a model to this evaluation first.
            </p>
          )}

          {titleError && <p className="text-destructive text-sm">{titleError}</p>}

          <Button onClick={handleStart} disabled={!canStart}>
            {createGroup.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" /> Starting…
              </>
            ) : (
              'Start conversation'
            )}
          </Button>
        </div>
      )}
    </div>
  )
}
