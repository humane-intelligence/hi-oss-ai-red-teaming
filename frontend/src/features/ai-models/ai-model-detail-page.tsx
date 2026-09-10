import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useAiModel } from './queries'
import { useClearApiKey, useDeleteModel, useRunHealthCheck, useUpdateModel } from './mutations'
import {
  healthClass,
  healthLabel,
  imageUnconfirmedHint,
  IMAGE_UNCONFIRMED_HINT_LABEL,
  IMAGE_UNCONFIRMED_LABEL,
  modalityLabel,
  NO_TEXT_OUTPUT_HINT,
  NO_TEXT_OUTPUT_LABEL,
  providerLabel,
  requiresInferenceEndpoint,
} from './labels'
import { ApiKeyDialog } from './api-key-dialog'
import { ModelActions } from './model-actions'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { StatTile } from '@/components/shared/stat-tile'
import { humanizeError } from '@/lib/api/problem'
import { Field } from '@/components/shared/field'
import { InfoHint } from '@/components/shared/info-hint'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { REVERSIBLE_DELETE_NOTE } from '@/lib/restore/restore'

export function AiModelDetailPage() {
  const { id } = useParams<{ id: string }>()
  const modelId = id ?? ''
  const navigate = useNavigate()
  const query = useAiModel(modelId)
  const model = query.data
  const update = useUpdateModel(modelId)
  const healthCheck = useRunHealthCheck(modelId)
  const clearKey = useClearApiKey(modelId)
  const del = useDeleteModel()
  const [apiKeyOpen, setApiKeyOpen] = useState(false)
  const [clearKeyOpen, setClearKeyOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/ai-models')}>
        <ArrowLeft className="size-4" /> Back
      </Button>

      {query.isPending && <DetailSkeleton />}
      {query.isError && <p className="text-destructive">{humanizeError(query.error)}</p>}

      {model && (
        <>
          <Breadcrumbs items={[{ label: 'AI models', to: '/ai-models' }, { label: model.name }]} />

          <header className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                <h1 className="font-display text-2xl font-semibold tracking-tight">{model.name}</h1>
                <p className="text-muted-foreground font-mono text-xs">
                  {providerLabel(model.provider)} · {model.provider_model_id}
                </p>
              </div>
              <ModelActions
                model={model}
                onEdit={() => navigate(`/ai-models/${model.id}/edit`)}
                onSetApiKey={() => setApiKeyOpen(true)}
                onToggleDisabled={() => update.mutate({ is_disabled: !model.is_disabled })}
                onHealthCheck={() => healthCheck.mutate()}
                onClearKey={() => setClearKeyOpen(true)}
                onDelete={() => setDeleteOpen(true)}
                togglePending={update.isPending}
                healthPending={healthCheck.isPending}
                clearKeyPending={clearKey.isPending}
              />
            </div>

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <StatTile
                label="Status"
                value={
                  <span className={model.is_disabled ? 'text-destructive' : 'text-ok'}>
                    {model.is_disabled ? 'Disabled' : 'Enabled'}
                  </span>
                }
              />
              <StatTile
                label="Health"
                value={
                  <span className="flex flex-col gap-0.5">
                    <span className={healthClass(model.health_check_status)}>
                      {healthLabel(model.health_check_status)}
                    </span>
                    {model.health_check_status === 'dead' && model.last_health_reason && (
                      <span className="text-muted-foreground text-xs font-normal">
                        {model.last_health_reason}
                      </span>
                    )}
                  </span>
                }
              />
              <StatTile
                label="API key"
                value={
                  model.has_api_key ? (
                    'Stored'
                  ) : (
                    <span className="text-muted-foreground">Not set</span>
                  )
                }
              />
            </div>
          </header>

          <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-3">
            <Card className="min-w-0 lg:col-span-2">
              <CardHeader>
                <CardTitle>Configuration</CardTitle>
              </CardHeader>
              <CardContent>
                <dl className="grid grid-cols-2 gap-4">
                  {/* Full width, and line breaks kept: the note is prose an admin typed, not a value. */}
                  <Field label="Description" className="col-span-2">
                    <span className="whitespace-pre-line">{model.description?.trim() || '—'}</span>
                  </Field>
                  <Field label="Alias">{model.model_alias}</Field>
                  <Field label="Labels">
                    {model.labels.length > 0 ? (
                      <span className="flex flex-wrap gap-1">
                        {model.labels.map((l) => (
                          <Badge key={l} variant="tag" className="max-w-[18rem]" title={l}>
                            <span className="truncate">{l}</span>
                          </Badge>
                        ))}
                      </span>
                    ) : (
                      '—'
                    )}
                  </Field>
                  {/* Both directions can carry a caveat, and both wear it the same way: a badge in
                      the value's own line, explanation in the hint. Prose in these cells used to
                      push one column five lines past the other and put unbounded vendor text on a
                      spec sheet. The caveat still sits with the declaration it qualifies — that is
                      where the operator repairs it — it just stops being a paragraph. */}
                  <Field label="Input">
                    <span className="flex flex-wrap items-center gap-1.5">
                      {model.input_modalities.map(modalityLabel).join(', ')}
                      {model.capability_mismatch && (
                        <>
                          <Badge variant="warn">{IMAGE_UNCONFIRMED_LABEL}</Badge>
                          <InfoHint
                            text={imageUnconfirmedHint(model.capability_mismatch)}
                            label={IMAGE_UNCONFIRMED_HINT_LABEL}
                          />
                        </>
                      )}
                    </span>
                  </Field>
                  <Field label="Output">
                    <span className="flex flex-wrap items-center gap-1.5">
                      {model.output_modalities.map(modalityLabel).join(', ')}
                      {!model.output_modalities.includes('text') && (
                        <>
                          <Badge variant="warn">{NO_TEXT_OUTPUT_LABEL}</Badge>
                          <InfoHint text={NO_TEXT_OUTPUT_HINT} />
                        </>
                      )}
                    </span>
                  </Field>
                  <Field label="Provider model id">{model.provider_model_id}</Field>
                  {/* Optional on most providers, so shown only once configured — but a
                      `generic` row without one cannot dispatch, so that gap stays visible. */}
                  {(model.inference_endpoint || requiresInferenceEndpoint(model.provider)) && (
                    <Field label="Inference endpoint">{model.inference_endpoint || '—'}</Field>
                  )}
                  {model.endpoint_name && (
                    <Field label="Endpoint name">{model.endpoint_name}</Field>
                  )}
                  <Field label="Warm up before chatting">
                    {model.warmup_enabled ? 'Yes' : 'No'}
                  </Field>
                  {(model.warmup_enabled || model.inactivity_alert_hours != null) && (
                    <Field label="Idle alert after">
                      {/* The sweep only checks warmed models, so a threshold without
                          warm-up is stored but inert — say so, or it reads as armed. */}
                      {model.inactivity_alert_hours
                        ? `${model.inactivity_alert_hours} h${model.warmup_enabled ? '' : ' (inactive — warm-up off)'}`
                        : 'Not set'}
                      {/* Mirrors the sweep's armed predicate: no nudge on a disabled model
                          (clicking Disable is the alert's own resolution) nor beside a
                          threshold the label just declared inert. */}
                      {model.inactivity_alerted_at &&
                        !model.is_disabled &&
                        model.warmup_enabled &&
                        model.inactivity_alert_hours != null && (
                          <span className="text-warn block text-xs">
                            Idle since{' '}
                            {model.last_used_at
                              ? new Date(model.last_used_at).toLocaleString()
                              : 'setup — never used'}
                            . Disable it if nobody needs it.
                          </span>
                        )}
                    </Field>
                  )}
                  <Field label="Last message">
                    {model.last_used_at ? new Date(model.last_used_at).toLocaleString() : '—'}
                  </Field>
                  <Field label="Last checked">
                    {model.last_health_check_at
                      ? new Date(model.last_health_check_at).toLocaleString()
                      : '—'}
                  </Field>
                  <Field label="Last healthy">
                    {model.last_healthy_at ? new Date(model.last_healthy_at).toLocaleString() : '—'}
                  </Field>
                  <Field label="Created">{new Date(model.created_at).toLocaleString()}</Field>
                </dl>
              </CardContent>
            </Card>

            <aside className="min-w-0">
              <Card>
                <CardHeader>
                  <CardTitle>Default parameters</CardTitle>
                </CardHeader>
                <CardContent>
                  {/* Stored knobs stay listed while the flag is on — they are kept, not deleted —
                      so say they are inert rather than showing them as live defaults. */}
                  {model.advanced_params_disabled && (
                    <p className="text-warn mb-3 text-xs">
                      Advanced parameters are disabled for this model: nothing below is sent, and no
                      layer under it can override them.
                    </p>
                  )}
                  {(() => {
                    const entries = Object.entries(model.parameters ?? {}).filter(
                      ([, v]) => v != null,
                    )
                    return entries.length > 0 ? (
                      <dl className="space-y-3">
                        {entries.map(([k, v]) => (
                          <Field key={k} label={k.replace(/_/g, ' ')}>
                            {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                          </Field>
                        ))}
                      </dl>
                    ) : (
                      <p className="text-muted-foreground text-sm">No default parameters set.</p>
                    )
                  })()}
                </CardContent>
              </Card>
            </aside>
          </div>

          <ApiKeyDialog modelId={model.id} open={apiKeyOpen} onOpenChange={setApiKeyOpen} />
          <ConfirmDialog
            open={clearKeyOpen}
            onOpenChange={setClearKeyOpen}
            title="Clear API key"
            description="Remove the stored API key? You'll need to set it again before this model can run."
            confirmLabel="Clear key"
            destructive
            pending={clearKey.isPending}
            onConfirm={() =>
              clearKey.mutate(undefined, { onSuccess: () => setClearKeyOpen(false) })
            }
          />
          <ConfirmDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            title="Delete model"
            description={`Delete “${model.name}”? ${REVERSIBLE_DELETE_NOTE}`}
            confirmLabel="Delete"
            destructive
            pending={del.isPending}
            onConfirm={() => del.mutate(model.id, { onSuccess: () => navigate('/ai-models') })}
          />
        </>
      )}
    </div>
  )
}
